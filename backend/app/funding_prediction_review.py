from __future__ import annotations

import json
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.funding_formation import (
    BITGET_FORMULA_VERSION,
    GATE_CONTROL_FORMULA_VERSION,
    GATE_SHADOW_FORMULA_VERSION,
    PREDICTION_LATEST_PREMIUM_WEIGHT,
    PREDICTION_METHOD,
    PREDICTION_MODEL_VERSION,
)
from app.models import (
    CryptoFundingFormationPredictionLog,
    CryptoFundingFormationWatchItem,
)
from app.system_runtime_log import append_system_runtime_event, record_exception, system_runtime_logs_overview
from app.funding_watch_health import health_overview, reset_health, update_health


PREDICTION_CHECKPOINT_MINUTES = (240, 180, 120, 60, 30, 15, 5)
DEFAULT_REVIEW_CHECKPOINT_MINUTES = 15
PREDICTION_CAPTURE_WINDOW_SECONDS = 90
SETTLEMENT_GRACE_SECONDS = 90
SETTLEMENT_MATCH_WINDOW_SECONDS = 300
PENDING_MAX_AGE_HOURS = 72
DEFAULT_SUCCESS_TOLERANCE_BPS = 1.0
SUPPORTED_EXCHANGES = {"bn", "by", "gt", "okx", "bg"}

_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None
_scheduler_lock = threading.Lock()


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _checkpoint_for_lead_seconds(lead_seconds: float) -> int | None:
    candidates = [
        checkpoint
        for checkpoint in PREDICTION_CHECKPOINT_MINUTES
        if abs(lead_seconds - checkpoint * 60) <= PREDICTION_CAPTURE_WINDOW_SECONDS
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda checkpoint: abs(lead_seconds - checkpoint * 60))


def _normalize_watch_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    exchange = str(raw.get("exchange") or "").strip().lower()
    symbol = str(raw.get("symbol") or "").strip().upper()
    if exchange not in SUPPORTED_EXCHANGES or not symbol:
        return None
    target_rate = _finite_float(raw.get("targetRate"))
    return {"exchange": exchange, "symbol": symbol, "targetRate": target_rate}


def _prediction_model_version(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("predictionModelVersion") or "").strip()
    if explicit:
        return explicit[:80]
    method = str(payload.get("predictionMethod") or "unknown").strip()
    if method == PREDICTION_METHOD:
        return PREDICTION_MODEL_VERSION
    if method == "latest_premium_carry_forward":
        return "funding_formation_v1_latest_tick"
    return method[:80] or "unknown"


def _calculation_details(payload: dict[str, Any]) -> dict[str, Any]:
    shadow = payload.get("shadowCalculation")
    rolling = payload.get("rollingReference")
    return {
        "formulaVersion": payload.get("formulaVersion"),
        "predictionModelVersion": _prediction_model_version(payload),
        "windowMode": payload.get("ruleWindowMode"),
        "aggregationMode": payload.get("aggregationMode"),
        "cycleStartTime": payload.get("cycleStartTime"),
        "settlementTime": payload.get("nextFundingTime"),
        "historyWindowStartTime": payload.get("historyWindowStartTime"),
        "historyWindowEndTime": payload.get("historyWindowEndTime"),
        "sampleCount": payload.get("coveredSamples"),
        "expectedElapsedSamples": payload.get("elapsedSamples"),
        "coverage": payload.get("coverage"),
        "weightedCoverage": payload.get("weightedCoverage"),
        "historySource": payload.get("historySource"),
        "historyPrecision": payload.get("historyPrecision"),
        "publicResolutionSeconds": payload.get("publicResolutionSeconds"),
        "officialSampleSeconds": payload.get("officialSampleSeconds"),
        "averagePremiumRate": payload.get("averagePremiumRate"),
        "latestPremiumRate": payload.get("latestPremiumRate"),
        "latestPremiumCandleTime": payload.get("latestPremiumCandleTime"),
        "recentPremiumMedianRate": payload.get("recentPremiumMedianRate"),
        "recentPremiumSampleCount": payload.get("recentPremiumSampleCount"),
        "robustPredictedFundingRate": payload.get("robustPredictedFundingRate"),
        "predictionSensitivityLow": payload.get("predictionSensitivityLow"),
        "predictionSensitivityHigh": payload.get("predictionSensitivityHigh"),
        "predictionConfidence": payload.get("predictionConfidence"),
        "futurePremiumAnchorRate": payload.get("futurePremiumAnchorRate"),
        "predictedAveragePremiumRate": payload.get("predictedAveragePremiumRate"),
        "interestRate": payload.get("interestRate"),
        "intervalScale": payload.get("intervalScale"),
        "fundingFloor": payload.get("effectiveFundingFloor"),
        "fundingCap": payload.get("effectiveFundingCap"),
        "predictedFundingRate": payload.get("predictedFundingRate"),
        "exchangePredictedRate": payload.get("currentFundingRate"),
        "rollingReference": rolling if isinstance(rolling, dict) else None,
        "shadowCalculation": shadow if isinstance(shadow, dict) else None,
    }


def _stored_calculation_details(row: CryptoFundingFormationPredictionLog) -> dict[str, Any]:
    if not row.calculation_details_json:
        return {}
    try:
        value = json.loads(row.calculation_details_json)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def sync_funding_formation_watches(
    db: Session,
    raw_items: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    normalized_by_route: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _normalize_watch_item(raw)
        if item is not None:
            normalized_by_route[(item["exchange"], item["symbol"])] = item

    existing = list(db.scalars(select(CryptoFundingFormationWatchItem)))
    existing_by_route = {(item.exchange, item.symbol): item for item in existing}
    now = datetime.now(timezone.utc)
    for item in existing:
        if not item.enabled and (item.exchange, item.symbol) in normalized_by_route:
            reset_health(db, item, now)
        item.enabled = (item.exchange, item.symbol) in normalized_by_route
        item.updated_at = now
    for route, raw in normalized_by_route.items():
        item = existing_by_route.get(route)
        if item is None:
            item = CryptoFundingFormationWatchItem(
                exchange=route[0],
                symbol=route[1],
                target_rate=raw["targetRate"],
                enabled=True,
            )
            db.add(item)
            reset_health(db, item, now)
        else:
            item.target_rate = raw["targetRate"]
            item.enabled = True
    db.commit()
    return funding_formation_watch_overview(db)


def funding_formation_watch_overview(db: Session) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(CryptoFundingFormationWatchItem)
            .where(CryptoFundingFormationWatchItem.enabled.is_(True))
            .order_by(CryptoFundingFormationWatchItem.exchange, CryptoFundingFormationWatchItem.symbol)
        )
    )
    return {
        "status": "ok",
        "updatedAt": datetime.now(timezone.utc),
        "itemCount": len(rows),
        "items": [
            {
                "exchange": row.exchange,
                "symbol": row.symbol,
                "targetRate": row.target_rate,
                "lastCheckedAt": row.last_checked_at,
                "lastError": row.last_error,
                "health": health_overview(db, row, datetime.now(timezone.utc)),
            }
            for row in rows
        ],
    }


def record_prediction_checkpoint(
    db: Session,
    payload: dict[str, Any],
    *,
    captured_at: datetime | None = None,
) -> CryptoFundingFormationPredictionLog | None:
    if payload.get("status") != "ok" or payload.get("predictionStatus") != "estimated":
        return None
    predicted_rate = _finite_float(payload.get("predictedFundingRate"))
    settlement_time = _utc_datetime(payload.get("nextFundingTime"))
    cycle_start_time = _utc_datetime(payload.get("cycleStartTime"))
    lead_minutes = _finite_float(payload.get("minutesToFunding"))
    if predicted_rate is None or settlement_time is None or cycle_start_time is None or lead_minutes is None:
        return None
    lead_seconds = max(0.0, lead_minutes * 60)
    checkpoint = _checkpoint_for_lead_seconds(lead_seconds)
    if checkpoint is None:
        return None
    exchange = str(payload.get("exchange") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if exchange not in SUPPORTED_EXCHANGES or not symbol:
        return None

    existing = db.scalar(
        select(CryptoFundingFormationPredictionLog).where(
            CryptoFundingFormationPredictionLog.exchange == exchange,
            CryptoFundingFormationPredictionLog.symbol == symbol,
            CryptoFundingFormationPredictionLog.settlement_time == settlement_time,
            CryptoFundingFormationPredictionLog.checkpoint_minutes == checkpoint,
        )
    )
    if existing is not None:
        return None

    row = CryptoFundingFormationPredictionLog(
        exchange=exchange,
        symbol=symbol,
        cycle_start_time=cycle_start_time,
        settlement_time=settlement_time,
        checkpoint_minutes=checkpoint,
        predicted_at=captured_at or datetime.now(timezone.utc),
        lead_seconds=lead_seconds,
        system_predicted_rate=predicted_rate,
        exchange_predicted_rate=_finite_float(payload.get("currentFundingRate")),
        predicted_average_premium_rate=_finite_float(payload.get("predictedAveragePremiumRate")),
        average_premium_rate=_finite_float(payload.get("averagePremiumRate")),
        latest_premium_rate=_finite_float(payload.get("latestPremiumRate")),
        coverage=_finite_float(payload.get("coverage")),
        weighted_coverage=_finite_float(payload.get("weightedCoverage")),
        prediction_method=str(payload.get("predictionMethod") or "unknown")[:80],
        prediction_model_version=_prediction_model_version(payload),
        formula_version=str(payload.get("formulaVersion") or "")[:80] or None,
        calculation_details_json=json.dumps(
            _calculation_details(payload),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ),
        shadow_formula_version=(
            str(payload.get("shadowCalculation", {}).get("formulaVersion") or "")[:80] or None
            if isinstance(payload.get("shadowCalculation"), dict)
            else None
        ),
        shadow_predicted_rate=(
            _finite_float(payload.get("shadowCalculation", {}).get("predictedFundingRate"))
            if isinstance(payload.get("shadowCalculation"), dict)
            else None
        ),
        funding_interval_hours=_finite_float(payload.get("fundingIntervalHours")),
        source_status=str(payload.get("accuracyStatus") or "ok")[:40],
        evaluation_status="pending",
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row


def record_prediction_batch(db: Session, batch_items: Iterable[dict[str, Any]]) -> int:
    recorded_rows: list[CryptoFundingFormationPredictionLog] = []
    for item in batch_items:
        if not isinstance(item, dict) or item.get("status") != "ok":
            continue
        data = item.get("data")
        if isinstance(data, dict):
            row = record_prediction_checkpoint(db, data)
            if row is not None:
                recorded_rows.append(row)
    db.commit()
    for row in recorded_rows:
        log_prediction_checkpoint(row)
    return len(recorded_rows)


def log_prediction_checkpoint(row: CryptoFundingFormationPredictionLog) -> None:
    calculation = _stored_calculation_details(row)
    append_system_runtime_event(
        "funding_prediction_checkpoint",
        level="info",
        module="funding_prediction",
        message=(
            f"{row.exchange.upper()} {row.symbol} 距结算{row.checkpoint_minutes}分钟预测已记录"
        ),
        details={
            "exchange": row.exchange,
            "symbol": row.symbol,
            "settlementTime": row.settlement_time,
            "checkpointMinutes": row.checkpoint_minutes,
            "systemPredictedRate": row.system_predicted_rate,
            "exchangePredictedRate": row.exchange_predicted_rate,
            "predictionMethod": row.prediction_method,
            "predictionModelVersion": row.prediction_model_version,
            "formulaVersion": row.formula_version,
            "coverage": row.coverage,
            "weightedCoverage": row.weighted_coverage,
            "calculation": calculation,
            "shadowFormulaVersion": row.shadow_formula_version,
            "shadowPredictedRate": row.shadow_predicted_rate,
        },
    )


def _history_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _direction(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _settlement_prediction_rate(row: CryptoFundingFormationPredictionLog, rate: float) -> float:
    """Score observation rates against the bounds saved with that prediction."""
    details = _stored_calculation_details(row)
    floor = _finite_float(details.get("fundingFloor"))
    cap = _finite_float(details.get("fundingCap"))
    if floor is not None and cap is not None and floor > cap:
        return rate
    if floor is not None:
        rate = max(rate, floor)
    if cap is not None:
        rate = min(rate, cap)
    return rate


def _apply_evaluation(
    row: CryptoFundingFormationPredictionLog,
    *,
    actual_rate: float,
    actual_time: datetime,
    evaluated_at: datetime,
    tolerance_bps: float = DEFAULT_SUCCESS_TOLERANCE_BPS,
) -> None:
    tolerance = tolerance_bps / 10_000
    scored_rate = _settlement_prediction_rate(row, row.system_predicted_rate)
    signed_error = scored_rate - actual_rate
    absolute_error = abs(signed_error)
    direction_hit = _direction(scored_rate) == _direction(actual_rate)
    row.actual_funding_rate = actual_rate
    row.actual_funding_time = actual_time
    row.signed_error = signed_error
    row.absolute_error = absolute_error
    row.direction_hit = direction_hit
    row.success_tolerance = tolerance
    row.success = direction_hit and absolute_error <= tolerance
    if row.exchange_predicted_rate is not None:
        exchange_error = abs(row.exchange_predicted_rate - actual_rate)
        row.exchange_absolute_error = exchange_error
        row.exchange_success = (
            _direction(row.exchange_predicted_rate) == _direction(actual_rate)
            and exchange_error <= tolerance
        )
    if row.shadow_predicted_rate is not None:
        shadow_rate = _settlement_prediction_rate(row, row.shadow_predicted_rate)
        shadow_error = abs(shadow_rate - actual_rate)
        row.shadow_absolute_error = shadow_error
        row.shadow_success = (
            _direction(shadow_rate) == _direction(actual_rate)
            and shadow_error <= tolerance
        )
    row.evaluation_status = "matched"
    row.evaluation_error = None
    row.evaluated_at = evaluated_at


def reconcile_pending_predictions(
    db: Session,
    fetch_history: Callable[[str, str, int], Iterable[Any]],
    *,
    now: datetime | None = None,
    limit: int = 500,
    emit_runtime_logs: bool = True,
) -> dict[str, int]:
    current = _utc_datetime(now) or datetime.now(timezone.utc)
    cutoff = current - timedelta(seconds=SETTLEMENT_GRACE_SECONDS)
    pending = list(
        db.scalars(
            select(CryptoFundingFormationPredictionLog)
            .where(
                CryptoFundingFormationPredictionLog.evaluation_status == "pending",
                CryptoFundingFormationPredictionLog.settlement_time <= cutoff,
            )
            .order_by(CryptoFundingFormationPredictionLog.settlement_time)
            .limit(limit)
        )
    )
    grouped: dict[tuple[str, str], list[CryptoFundingFormationPredictionLog]] = {}
    for row in pending:
        grouped.setdefault((row.exchange, row.symbol), []).append(row)

    matched = 0
    unavailable = 0
    matched_rows: list[CryptoFundingFormationPredictionLog] = []
    for (exchange, symbol), rows in grouped.items():
        try:
            history = list(fetch_history(exchange, symbol, 100))
            fetch_error = None
        except Exception as exc:  # noqa: BLE001
            history = []
            fetch_error = str(exc)
        candidates: list[tuple[datetime, float]] = []
        for item in history:
            funding_time = _utc_datetime(_history_field(item, "funding_time"))
            funding_rate = _finite_float(_history_field(item, "funding_rate"))
            if funding_time is not None and funding_rate is not None:
                candidates.append((funding_time, funding_rate))
        for row in rows:
            row.evaluation_attempts += 1
            row.last_evaluation_attempt_at = current
            target = _utc_datetime(row.settlement_time)
            if target is None:
                row.evaluation_error = "结算时间无效"
                continue
            match = min(candidates, key=lambda item: abs((item[0] - target).total_seconds()), default=None)
            if match is not None and abs((match[0] - target).total_seconds()) <= SETTLEMENT_MATCH_WINDOW_SECONDS:
                _apply_evaluation(
                    row,
                    actual_rate=match[1],
                    actual_time=match[0],
                    evaluated_at=current,
                )
                matched += 1
                matched_rows.append(row)
                continue
            age_hours = max(0.0, (current - target).total_seconds() / 3600)
            row.evaluation_error = fetch_error or "交易所结算历史暂未返回该周期"
            if age_hours >= PENDING_MAX_AGE_HOURS:
                row.evaluation_status = "unavailable"
                row.evaluated_at = current
                unavailable += 1
    db.commit()
    if emit_runtime_logs:
        for row in matched_rows:
            append_system_runtime_event(
                "funding_prediction_evaluated",
                level="info" if row.success else "warning",
                module="funding_prediction",
                message=(
                    f"{row.exchange.upper()} {row.symbol} {row.checkpoint_minutes}分钟预测"
                    f"{'命中' if row.success else '偏差超限'}"
                ),
                details={
                    "exchange": row.exchange,
                    "symbol": row.symbol,
                    "settlementTime": row.settlement_time,
                    "checkpointMinutes": row.checkpoint_minutes,
                    "systemPredictedRate": row.system_predicted_rate,
                    "exchangePredictedRate": row.exchange_predicted_rate,
                    "actualFundingRate": row.actual_funding_rate,
                    "absoluteError": row.absolute_error,
                    "signedError": row.signed_error,
                    "directionHit": row.direction_hit,
                    "success": row.success,
                    "predictionMethod": row.prediction_method,
                    "predictionModelVersion": row.prediction_model_version,
                    "formulaVersion": row.formula_version,
                    "calculation": _stored_calculation_details(row),
                    "shadowFormulaVersion": row.shadow_formula_version,
                    "shadowPredictedRate": row.shadow_predicted_rate,
                    "shadowAbsoluteError": row.shadow_absolute_error,
                    "shadowSuccess": row.shadow_success,
                },
            )
    return {"checked": len(pending), "matched": matched, "unavailable": unavailable}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None



def _bounds_status(row: CryptoFundingFormationPredictionLog) -> str:
    details = _stored_calculation_details(row)
    floor, cap = (_finite_float(details.get(key)) for key in ("fundingFloor", "fundingCap"))
    if floor is None or cap is None:
        return "missing" if floor is None and cap is None else "partial"
    return "complete" if floor <= cap else "invalid"


def _original_evaluations(rows: list[CryptoFundingFormationPredictionLog]) -> dict[int, dict[str, Any]]:
    """Join original append-only events with current review rows; never rewrite them."""
    if not rows:
        return {}
    def key(exchange, symbol, settlement, checkpoint):
        at = _utc_datetime(settlement)
        return (exchange, symbol, at, checkpoint)
    lookup = {key(r.exchange, r.symbol, r.settlement_time, r.checkpoint_minutes): r.id for r in rows}
    events = system_runtime_logs_overview(
        500, module="funding_prediction", event="funding_prediction_evaluated",
    )["items"]
    result = {}
    for event in events:
        detail = event.get("details") or {}
        row_id = lookup.get(key(detail.get("exchange"), detail.get("symbol"),
                                detail.get("settlementTime"), detail.get("checkpointMinutes")))
        if row_id is not None:
            result[row_id] = {"at": event.get("at"), "message": event.get("message"),
                              "success": detail.get("success"), "absoluteError": detail.get("absoluteError")}
    return result

def _formula_success_breakdown(
    rows: Iterable[CryptoFundingFormationPredictionLog],
    *,
    shadow: bool = False,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, int], list[CryptoFundingFormationPredictionLog]] = {}
    for row in rows:
        if row.evaluation_status != "matched" or row.actual_funding_rate is None or _bounds_status(row) != "complete":
            continue
        formula_version = row.shadow_formula_version if shadow else row.formula_version
        success = row.shadow_success if shadow else row.success
        error = row.shadow_absolute_error if shadow else row.absolute_error
        if not formula_version or success is None or error is None:
            continue
        grouped.setdefault(
            (row.exchange, row.prediction_model_version or "unknown", formula_version, row.checkpoint_minutes),
            [],
        ).append(row)

    result: list[dict[str, Any]] = []
    for (exchange, model_version, formula_version, checkpoint), group in sorted(grouped.items()):
        successes = [
            row.shadow_success if shadow else row.success
            for row in group
        ]
        errors = [
            row.shadow_absolute_error if shadow else row.absolute_error
            for row in group
        ]
        numeric_errors = [float(value) for value in errors if value is not None]
        hit_count = len([value for value in successes if value])
        result.append(
            {
                "exchange": exchange,
                "formulaVersion": formula_version,
                "modelVersion": model_version,
                "checkpointMinutes": checkpoint,
                "sampleCount": len(group),
                "hitCount": hit_count,
                "hitRate": hit_count / len(group),
                "meanAbsoluteError": _mean(numeric_errors),
                "shadow": shadow,
            }
        )
    return result


def funding_prediction_review_overview(
    db: Session,
    *,
    days: int = 30,
    checkpoint_minutes: int = DEFAULT_REVIEW_CHECKPOINT_MINUTES,
    exchange: str | None = None,
    symbol: str | None = None,
    model_version: str | None = PREDICTION_MODEL_VERSION,
    limit: int = 100,
) -> dict[str, Any]:
    if checkpoint_minutes not in PREDICTION_CHECKPOINT_MINUTES:
        raise ValueError("预测提前量仅支持 240、180、120、60、30、15、5 分钟")
    days = min(max(int(days), 1), 365)
    limit = min(max(int(limit), 1), 300)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    statement = select(CryptoFundingFormationPredictionLog).where(
        CryptoFundingFormationPredictionLog.checkpoint_minutes == checkpoint_minutes,
        CryptoFundingFormationPredictionLog.settlement_time >= cutoff,
    )
    normalized_exchange = str(exchange or "").strip().lower()
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_exchange:
        statement = statement.where(CryptoFundingFormationPredictionLog.exchange == normalized_exchange)
    if normalized_symbol:
        statement = statement.where(CryptoFundingFormationPredictionLog.symbol == normalized_symbol)
    normalized_model_version = str(
        model_version if model_version is not None else PREDICTION_MODEL_VERSION
    ).strip()
    if normalized_model_version and normalized_model_version != "all":
        statement = statement.where(
            CryptoFundingFormationPredictionLog.prediction_model_version == normalized_model_version
        )
    rows = list(
        db.scalars(
            statement.order_by(desc(CryptoFundingFormationPredictionLog.settlement_time), desc(CryptoFundingFormationPredictionLog.id))
        )
    )
    settled = [row for row in rows if row.evaluation_status == "matched" and row.actual_funding_rate is not None]
    # Re-score historical matches too; their stored errors may predate bound-aware review.
    for row in settled:
        _apply_evaluation(
            row, actual_rate=row.actual_funding_rate,
            actual_time=row.actual_funding_time or row.settlement_time,
            evaluated_at=row.evaluated_at or datetime.now(timezone.utc),
            tolerance_bps=(row.success_tolerance * 10_000
                           if row.success_tolerance is not None else DEFAULT_SUCCESS_TOLERANCE_BPS),
        )
    all_settled = settled
    legacy = [row for row in settled if _bounds_status(row) != "complete"]
    settled = [row for row in settled if _bounds_status(row) == "complete"]
    original = _original_evaluations(rows)
    corrected_ids = {row.id for row in rows if row.evaluation_status == "matched" and row.id in original
                     and original[row.id].get("success") is not None
                     and original[row.id]["success"] != row.success}
    # Bring corrected warnings to the visible detail list, even if older than its cap.
    detail_rows = sorted(rows, key=lambda row: row.id not in corrected_ids)[:limit]
    pending = [row for row in rows if row.evaluation_status == "pending"]
    unavailable = [row for row in rows if row.evaluation_status == "unavailable"]
    errors = [row.absolute_error for row in settled if row.absolute_error is not None]
    signed_errors = [row.signed_error for row in settled if row.signed_error is not None]
    exchange_errors = [row.exchange_absolute_error for row in settled if row.exchange_absolute_error is not None]
    hits = len([row for row in settled if row.success])
    direction_hits = len([row for row in settled if row.direction_hit])
    exchange_hits = len([row for row in settled if row.exchange_success])
    exchange_sample_count = len([row for row in settled if row.exchange_success is not None])
    return {
        "status": "ok",
        "updatedAt": datetime.now(timezone.utc),
        "windowDays": days,
        "checkpointMinutes": checkpoint_minutes,
        "modelVersion": normalized_model_version or "all",
        "activeModelVersion": PREDICTION_MODEL_VERSION,
        "availableCheckpoints": list(PREDICTION_CHECKPOINT_MINUTES),
        "successDefinition": "预测先按记录时的交易所上下限折算，再与实际结算比较：方向一致且误差不超过 1bp（0.01 个百分点）即命中。超上限或低于下限且实际在对应边界时算准确；边界缺失或无效的历史记录单列，不计入成功率。",
        "sampleStatus": "ok" if len(settled) >= 30 else "insufficient",
        "settledCount": len(all_settled),
        "scoredCount": len(settled),
        "legacyCount": len(legacy),
        "legacyHitRate": sum(bool(row.success) for row in legacy) / len(legacy) if legacy else None,
        "correctedLogCount": len(corrected_ids),
        "totalRecordCount": len(rows),
        "detailLimit": limit,
        "pendingCount": len(pending),
        "unavailableCount": len(unavailable),
        "hitCount": hits,
        "hitRate": hits / len(settled) if settled else None,
        "directionHitRate": direction_hits / len(settled) if settled else None,
        "meanAbsoluteError": _mean(errors),
        "medianAbsoluteError": statistics.median(errors) if errors else None,
        "meanSignedError": _mean(signed_errors),
        "exchangeHitRate": exchange_hits / exchange_sample_count if exchange_sample_count else None,
        "exchangeMeanAbsoluteError": _mean(exchange_errors),
        "breakdown": _formula_success_breakdown(rows),
        "shadowBreakdown": _formula_success_breakdown(rows, shadow=True),
        "items": [
            {
                "id": row.id,
                "exchange": row.exchange,
                "symbol": row.symbol,
                "settlementTime": row.settlement_time,
                "checkpointMinutes": row.checkpoint_minutes,
                "predictedAt": row.predicted_at,
                "leadSeconds": row.lead_seconds,
                "systemPredictedRate": row.system_predicted_rate,
                "settlementPredictedRate": _settlement_prediction_rate(row, row.system_predicted_rate),
                "boundsStatus": _bounds_status(row),
                "includedInAccuracy": row.evaluation_status == "matched" and _bounds_status(row) == "complete",
                "originalEvaluation": original.get(row.id),
                "scoreCorrected": row.id in corrected_ids,
                "exchangePredictedRate": row.exchange_predicted_rate,
                "actualFundingRate": row.actual_funding_rate,
                "absoluteError": row.absolute_error,
                "directionHit": row.direction_hit,
                "success": row.success,
                "evaluationStatus": row.evaluation_status,
                "evaluationError": row.evaluation_error,
                "coverage": row.coverage,
                "formulaVersion": row.formula_version,
                "predictionModelVersion": row.prediction_model_version,
                "shadowFormulaVersion": row.shadow_formula_version,
                "shadowPredictedRate": row.shadow_predicted_rate,
                "shadowAbsoluteError": row.shadow_absolute_error,
                "shadowSuccess": row.shadow_success,
                "calculationDetails": _stored_calculation_details(row),
            }
            for row in detail_rows
        ],
    }


def run_funding_prediction_scan() -> dict[str, int]:
    if not _scheduler_lock.acquire(blocking=False):
        return {"watched": 0, "recorded": 0, "matched": 0}
    try:
        from app.crypto import fetch_funding_history, funding_formation_overview

        with SessionLocal() as db:
            watches = list(
                db.scalars(
                    select(CryptoFundingFormationWatchItem)
                    .where(CryptoFundingFormationWatchItem.enabled.is_(True))
                    .order_by(CryptoFundingFormationWatchItem.id)
                    .limit(25)
                )
            )
            routes = [
                {
                    "id": watch.id,
                    "exchange": watch.exchange,
                    "symbol": watch.symbol,
                    "targetRate": watch.target_rate,
                }
                for watch in watches
            ]

        results: dict[int, tuple[dict[str, Any] | None, str | None]] = {}
        if routes:
            with ThreadPoolExecutor(max_workers=min(4, len(routes))) as executor:
                future_map = {
                    executor.submit(
                        funding_formation_overview,
                        route["exchange"],
                        route["symbol"],
                        route["targetRate"],
                    ): route
                    for route in routes
                }
                for future in as_completed(future_map):
                    route = future_map[future]
                    try:
                        results[route["id"]] = (future.result(), None)
                    except Exception as exc:  # noqa: BLE001
                        results[route["id"]] = (None, str(exc))

        recorded_rows: list[CryptoFundingFormationPredictionLog] = []
        with SessionLocal() as db:
            now = datetime.now(timezone.utc)
            for route in routes:
                watch = db.get(CryptoFundingFormationWatchItem, route["id"])
                data, error = results.get(route["id"], (None, "未返回结果"))
                if watch is not None:
                    watch.last_checked_at = now
                    watch.last_error = error
                if data is not None:
                    row = record_prediction_checkpoint(db, data, captured_at=now)
                    if row is not None:
                        recorded_rows.append(row)
                if watch is not None:
                    update_health(db, watch, data, error, now)
            db.commit()
            for row in recorded_rows:
                log_prediction_checkpoint(row)
            reconciliation = reconcile_pending_predictions(db, fetch_funding_history, now=now)
        return {
            "watched": len(routes),
            "recorded": len(recorded_rows),
            "matched": reconciliation["matched"],
        }
    except Exception as exc:  # noqa: BLE001
        record_exception(exc, event="funding_prediction_scan_failed", module="funding_prediction")
        return {"watched": 0, "recorded": 0, "matched": 0}
    finally:
        _scheduler_lock.release()


def _scheduler_loop() -> None:
    if _scheduler_stop.wait(8):
        return
    while not _scheduler_stop.is_set():
        run_funding_prediction_scan()
        if _scheduler_stop.wait(60):
            return


def start_funding_prediction_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="funding-prediction-review",
        daemon=True,
    )
    _scheduler_thread.start()
    append_system_runtime_event(
        "funding_prediction_model_active",
        level="info",
        module="funding_prediction",
        message="资金费公式与预测模型已启用，后续成功率按交易所、公式版本和检查点独立统计",
        details={
            "modelVersion": PREDICTION_MODEL_VERSION,
            "predictionMethod": PREDICTION_METHOD,
            "latestPremiumWeight": PREDICTION_LATEST_PREMIUM_WEIGHT,
            "formedAverageWeight": 1.0 - PREDICTION_LATEST_PREMIUM_WEIGHT,
            "checkpointsMinutes": list(PREDICTION_CHECKPOINT_MINUTES),
            "successToleranceBps": DEFAULT_SUCCESS_TOLERANCE_BPS,
            "formulaVersions": {
                "bitget": BITGET_FORMULA_VERSION,
                "gateControl": GATE_CONTROL_FORMULA_VERSION,
                "gateShadow": GATE_SHADOW_FORMULA_VERSION,
            },
            "calculationDetailsLogged": True,
        },
    )


def stop_funding_prediction_scheduler() -> None:
    _scheduler_stop.set()
    thread = _scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=3)

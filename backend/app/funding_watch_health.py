"""Persist checkpoint expectations so restarts do not hide missed samples."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Integer, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.models import CryptoFundingFormationPredictionLog, CryptoFundingFormationWatchItem
from app.system_runtime_log import append_system_runtime_event


class FundingWatchHealth(Base):
    __tablename__ = "crypto_funding_formation_watch_health"
    watch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


def utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def reset_health(db: Session, watch: CryptoFundingFormationWatchItem, now: datetime) -> None:
    db.flush()
    row = db.get(FundingWatchHealth, watch.id)
    if row is None:
        row = FundingWatchHealth(watch_id=watch.id)
        db.add(row)
    row.state_json = json.dumps({"startedAt": now.isoformat(), "expected": []})
    # Production sessions use autoflush=False; subsequent db.get must see new rows.
    db.flush()


def _state(db: Session, watch: CryptoFundingFormationWatchItem) -> dict[str, Any]:
    row = db.get(FundingWatchHealth, watch.id)
    return json.loads(row.state_json) if row else {}


def _checkpoints(db: Session, watch: CryptoFundingFormationWatchItem, state: dict, now: datetime) -> list[dict]:
    from app.funding_prediction_review import PREDICTION_CAPTURE_WINDOW_SECONDS

    rows = db.scalars(select(CryptoFundingFormationPredictionLog).where(
        CryptoFundingFormationPredictionLog.exchange == watch.exchange,
        CryptoFundingFormationPredictionLog.symbol == watch.symbol,
        CryptoFundingFormationPredictionLog.settlement_time >= now - timedelta(days=2),
    )).all()
    recorded = {(utc(r.settlement_time).isoformat(), r.checkpoint_minutes): r for r in rows}
    result = []
    for entry in state.get("expected", []):
        item = dict(entry)
        row = recorded.get((item["settlementTime"], item["minutes"]))
        due = utc(item["at"])
        item["status"] = ("recorded" if row else "missed" if now > due + timedelta(seconds=PREDICTION_CAPTURE_WINDOW_SECONDS)
                          else "capturing" if now >= due - timedelta(seconds=PREDICTION_CAPTURE_WINDOW_SECONDS) else "waiting")
        item["recordedAt"] = utc(row.predicted_at).isoformat() if row else None
        result.append(item)
    return result


def update_health(db: Session, watch: CryptoFundingFormationWatchItem, payload: dict | None,
                  error: str | None, now: datetime) -> None:
    from app.funding_prediction_review import PREDICTION_CHECKPOINT_MINUTES, PREDICTION_CAPTURE_WINDOW_SECONDS

    state = _state(db, watch)
    if not state:
        # Existing watches have no reliable activation history. Start monitoring
        # prospectively on upgrade instead of inventing old missed checkpoints.
        reset_health(db, watch, now)
        state = _state(db, watch)
    fresh = payload is not None and payload.get("status") == "ok" and not payload.get("stale")
    state["sourceError"] = error or (str(payload.get("errorMessage") or "采集结果不可用") if payload and not fresh else None)
    state["lastCheckedAt"] = now.isoformat()
    state["expected"] = [e for e in state.get("expected", []) if utc(e["at"]) >= now - timedelta(days=1)]
    if fresh:
        state["lastSuccessfulAt"] = (utc(payload.get("updatedAt")) or now).isoformat()
        end, start = utc(payload.get("nextFundingTime")), utc(payload.get("cycleStartTime"))
        if end and start and end > now:
            previous_end = utc(state.get("settlementTime"))
            if previous_end and previous_end > now and previous_end != end:
                # Exchange changed the scheduled settlement. Cancel expectations
                # for the superseded cycle rather than emitting phantom misses.
                state["expected"] = [e for e in state["expected"] if utc(e["settlementTime"]) != previous_end]
            state["settlementTime"] = end.isoformat()
            state["intervalSeconds"] = (end - start).total_seconds()
            entries = { (e["settlementTime"], e["minutes"]): e for e in state.get("expected", [])
                       if utc(e["at"]) >= now - timedelta(days=1) }
            started = utc(state["startedAt"])
            for minutes in PREDICTION_CHECKPOINT_MINUTES:
                due = end - timedelta(minutes=minutes)
                if due < start or due + timedelta(seconds=PREDICTION_CAPTURE_WINDOW_SECONDS) < max(started, now):
                    continue
                entries.setdefault((end.isoformat(), minutes), {
                    "at": due.isoformat(), "settlementTime": end.isoformat(), "minutes": minutes,
                })
            state["expected"] = sorted(entries.values(), key=lambda e: e["at"])
    state["predictionStatus"] = payload.get("predictionStatus") if payload else None
    # Flush checkpoint inserts in this transaction before deciding they were missed.
    db.flush()
    checks = _checkpoints(db, watch, state, now)
    for entry in state.get("expected", []):
        check = next((c for c in checks if c["at"] == entry["at"] and c["minutes"] == entry["minutes"]), None)
        if check and check["status"] == "missed" and not entry.get("alerted"):
            append_system_runtime_event(
                "funding_prediction_checkpoint_missed", level="warning", module="funding_prediction",
                message=f"{watch.exchange.upper()} {watch.symbol} 结算前{entry['minutes']}分钟预测漏记",
                details={"exchange": watch.exchange, "symbol": watch.symbol, **entry},
            )
            entry["alerted"] = True
    db.get(FundingWatchHealth, watch.id).state_json = json.dumps(state, ensure_ascii=False)


def health_overview(db: Session, watch: CryptoFundingFormationWatchItem, now: datetime) -> dict[str, Any]:
    state = _state(db, watch)
    checks = _checkpoints(db, watch, state, now)
    pending = [e for e in checks if e["status"] in {"waiting", "capturing"}]
    missed = [e for e in checks if e["status"] == "missed"]
    records = [e for e in checks if e["status"] == "recorded"]
    upcoming = pending[0] if pending else None
    # After the last checkpoint, display the next cycle's expected start. Only
    # an observed cycle is enrolled for missed-checkpoint checks.
    end = utc(state.get("settlementTime"))
    interval = state.get("intervalSeconds", 0)
    if upcoming is None and end and end > now and interval >= 300:
        from app.funding_prediction_review import PREDICTION_CHECKPOINT_MINUTES
        minutes = max(m for m in PREDICTION_CHECKPOINT_MINUTES if m * 60 <= interval)
        upcoming = {"at": (end + timedelta(seconds=interval, minutes=-minutes)).isoformat(), "minutes": minutes}
    success_at = utc(state.get("lastSuccessfulAt"))
    checked_at = utc(watch.last_checked_at)
    stale = success_at is not None and (now - success_at).total_seconds() > 180
    if not state or (success_at is None and not state.get("sourceError")):
        status, message = "initializing", "等待首次采集"
        baseline = checked_at or utc(state.get("startedAt")) or utc(watch.created_at)
        if baseline and (now - baseline).total_seconds() > 180:
            status, message = "stale", "采集超过3分钟未更新"
    elif state.get("sourceError") or watch.last_error:
        status, message = "error", "采集异常"
    elif stale:
        status, message = "stale", "采集超过3分钟未更新"
    elif missed:
        status, message = "missed", f"最近24小时漏记 {len(missed)} 个预测点"
    elif state.get("predictionStatus") == "insufficient":
        status, message = "insufficient", "溢价样本不足，暂不能记录预测"
    elif upcoming and any(e["status"] == "capturing" for e in pending):
        status, message = "capturing", "正在记录预测"
    else:
        status, message = "waiting", "采集正常，等待下一记录点"
    return {
        "status": status, "message": message,
        "lastSuccessfulAt": state.get("lastSuccessfulAt"),
        "nextCheckpointAt": upcoming["at"] if upcoming else None,
        "nextCheckpointMinutes": upcoming["minutes"] if upcoming else None,
        "lastRecordedAt": max((r["recordedAt"] for r in records), default=None),
        "missedCount": len(missed), "missedCheckpoints": missed,
        "error": state.get("sourceError") or watch.last_error,
    }

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KSTR_RUNTIME_LOG_MAX_BYTES = 2 * 1024 * 1024
KSTR_RUNTIME_LOG_MAX_ITEMS = 1000

_log_lock = threading.Lock()
_last_event_at: dict[str, float] = {}


def runtime_log_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    from app.database import get_data_dir

    return get_data_dir() / "kstr-runtime" / "events.jsonl"


def _clean_text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    return text[:limit] if text else None


def _safe_details(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)[:80]
        if isinstance(item, datetime):
            safe[name] = item.astimezone(timezone.utc).isoformat()
        elif isinstance(item, (str, int, float, bool)) or item is None:
            safe[name] = _clean_text(item, 500) if isinstance(item, str) else item
        elif isinstance(item, (list, tuple)):
            safe[name] = [
                _clean_text(entry, 200) if isinstance(entry, str) else entry
                for entry in list(item)[:30]
                if isinstance(entry, (str, int, float, bool)) or entry is None
            ]
    return safe


def _rotate_if_needed(target: Path) -> None:
    try:
        if target.stat().st_size < KSTR_RUNTIME_LOG_MAX_BYTES:
            return
    except OSError:
        return
    rotated = target.with_suffix(target.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        target.replace(rotated)
    except OSError:
        return


def append_kstr_runtime_event(
    event: str,
    *,
    level: str = "info",
    stage: str = "runtime",
    status: str = "ok",
    etf_code: str | None = None,
    message: str | None = None,
    duration_ms: float | None = None,
    details: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    min_interval_seconds: float = 0,
    path: Path | None = None,
) -> dict[str, Any] | None:
    now_monotonic = time.monotonic()
    normalized_dedupe_key = _clean_text(dedupe_key, 160)
    with _log_lock:
        if normalized_dedupe_key and min_interval_seconds > 0:
            previous = _last_event_at.get(normalized_dedupe_key)
            if previous is not None and now_monotonic - previous < min_interval_seconds:
                return None
            _last_event_at[normalized_dedupe_key] = now_monotonic

        payload = {
            "id": str(time.time_ns()),
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": _clean_text(event, 100) or "unknown",
            "level": level if level in {"info", "warning", "error"} else "info",
            "stage": _clean_text(stage, 80) or "runtime",
            "status": _clean_text(status, 80) or "ok",
            "etfCode": _clean_text(etf_code, 16),
            "message": _clean_text(message),
            "durationMs": round(float(duration_ms), 1) if duration_ms is not None else None,
            "details": _safe_details(details),
        }
        target = runtime_log_path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(target)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            return None
    return payload


def _read_jsonl(target: Path) -> list[dict[str, Any]]:
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-KSTR_RUNTIME_LOG_MAX_ITEMS:]:
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def kstr_runtime_logs_overview(
    limit: int = 100,
    *,
    level: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    target = runtime_log_path(path)
    rotated = target.with_suffix(target.suffix + ".1")
    rows = [*_read_jsonl(rotated), *_read_jsonl(target)]
    normalized_level = str(level or "").strip().lower()
    if normalized_level in {"info", "warning", "error"}:
        rows = [row for row in rows if row.get("level") == normalized_level]
    rows.sort(
        key=lambda row: (str(row.get("at") or ""), str(row.get("id") or "")),
        reverse=True,
    )
    bounded_limit = max(1, min(int(limit), 500))
    items = rows[:bounded_limit]
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_rows = []
    for row in rows:
        try:
            at = datetime.fromisoformat(str(row.get("at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at.astimezone(timezone.utc) >= recent_cutoff:
            recent_rows.append(row)
    last_error = next((row for row in rows if row.get("level") == "error"), None)
    last_success = next((row for row in rows if row.get("status") == "ok"), None)
    return {
        "status": "ok",
        "count": len(items),
        "summary": {
            "lastHourCount": len(recent_rows),
            "lastHourErrors": sum(1 for row in recent_rows if row.get("level") == "error"),
            "lastSuccessAt": last_success.get("at") if last_success else None,
            "lastErrorAt": last_error.get("at") if last_error else None,
            "lastError": last_error.get("message") if last_error else None,
        },
        "items": items,
    }

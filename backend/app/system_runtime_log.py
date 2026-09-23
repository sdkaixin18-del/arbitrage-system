from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/system/runtime-logs", tags=["system-runtime-logs"])

_write_lock = threading.Lock()
_max_file_bytes = 5 * 1024 * 1024
_backup_count = 4
_max_text_length = 8_000
_retention_days = 14
_original_sys_excepthook = sys.excepthook
_original_threading_excepthook = threading.excepthook
_hooks_installed = False


def runtime_log_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    from app.database import get_data_dir

    return get_data_dir() / "system-runtime" / "events.jsonl"


def _trim_text(value: Any, limit: int = _max_text_length) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _safe_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 4:
        return _trim_text(value, 500)
    if isinstance(value, str):
        return _trim_text(value)
    if isinstance(value, dict):
        return {
            _trim_text(key, 100): _safe_value(item, depth + 1)
            for key, item in list(value.items())[:50]
            if str(key).lower() not in {"authorization", "cookie", "password", "token", "secret"}
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth + 1) for item in list(value)[:50]]
    return _trim_text(value)


def _rotate_if_needed(target: Path) -> None:
    if not target.exists() or target.stat().st_size < _max_file_bytes:
        return
    oldest = target.with_name(f"{target.name}.{_backup_count}")
    oldest.unlink(missing_ok=True)
    for index in range(_backup_count - 1, 0, -1):
        source = target.with_name(f"{target.name}.{index}")
        if source.exists():
            source.replace(target.with_name(f"{target.name}.{index + 1}"))
    target.replace(target.with_name(f"{target.name}.1"))


def append_system_runtime_event(
    event: str,
    *,
    level: str = "error",
    source: str = "backend",
    module: str | None = None,
    message: str | None = None,
    path: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    duration_ms: float | None = None,
    error_type: str | None = None,
    details: dict[str, Any] | None = None,
    target_path: Path | None = None,
) -> dict[str, Any]:
    payload = {
        "id": str(time.time_ns()),
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": _trim_text(event, 100),
        "level": level if level in {"info", "warning", "error"} else "error",
        "source": _trim_text(source, 100),
        "module": _trim_text(module, 150) or None,
        "message": _trim_text(message),
        "path": _trim_text(path, 500) or None,
        "method": _trim_text(method, 20) or None,
        "statusCode": status_code,
        "durationMs": round(duration_ms, 1) if duration_ms is not None else None,
        "errorType": _trim_text(error_type, 200) or None,
        "details": _safe_value(details or {}),
    }
    target = runtime_log_path(target_path)
    try:
        with _write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(target)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        # Diagnostics must never break the business request they are observing.
        pass
    return payload


def record_exception(
    exc: BaseException,
    *,
    event: str = "unhandled_exception",
    source: str = "backend",
    module: str | None = None,
    path: str | None = None,
    method: str | None = None,
    duration_ms: float | None = None,
    details: dict[str, Any] | None = None,
    target_path: Path | None = None,
) -> dict[str, Any]:
    exception_details = dict(details or {})
    exception_details["traceback"] = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    return append_system_runtime_event(
        event,
        level="error",
        source=source,
        module=module,
        message=str(exc) or type(exc).__name__,
        path=path,
        method=method,
        duration_ms=duration_ms,
        error_type=type(exc).__name__,
        details=exception_details,
        target_path=target_path,
    )


def install_global_exception_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return

    def system_exception_hook(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            _original_sys_excepthook(exc_type, exc_value, exc_traceback)
            return
        exc_value = exc_value.with_traceback(exc_traceback)
        record_exception(exc_value, event="process_uncaught_exception", module="process")
        _original_sys_excepthook(exc_type, exc_value, exc_traceback)

    def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            exc_value = args.exc_value.with_traceback(args.exc_traceback)
            record_exception(
                exc_value,
                event="background_thread_exception",
                module=args.thread.name if args.thread else "background-thread",
            )
        _original_threading_excepthook(args)

    sys.excepthook = system_exception_hook
    threading.excepthook = thread_exception_hook
    _hooks_installed = True


def uninstall_global_exception_hooks() -> None:
    global _hooks_installed
    if not _hooks_installed:
        return
    sys.excepthook = _original_sys_excepthook
    threading.excepthook = _original_threading_excepthook
    _hooks_installed = False


def _iter_log_paths(target: Path) -> list[Path]:
    return [
        candidate
        for candidate in [target, *(target.with_name(f"{target.name}.{index}") for index in range(1, _backup_count + 1))]
        if candidate.exists()
    ]


def system_runtime_logs_overview(
    limit: int = 100,
    *,
    level: str | None = None,
    source: str | None = None,
    module: str | None = None,
    event: str | None = None,
    symbol: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    target = runtime_log_path(path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=_retention_days)
    normalized_event = str(event or "").strip().lower()
    normalized_symbol = str(symbol or "").strip().upper()
    if start_at is not None:
        start_at = start_at.replace(tzinfo=timezone.utc) if start_at.tzinfo is None else start_at.astimezone(timezone.utc)
    if end_at is not None:
        end_at = end_at.replace(tzinfo=timezone.utc) if end_at.tzinfo is None else end_at.astimezone(timezone.utc)
    items: list[dict[str, Any]] = []
    for log_path in _iter_log_paths(target):
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
                at = datetime.fromisoformat(str(item.get("at", "")).replace("Z", "+00:00"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if at < cutoff:
                continue
            if start_at is not None and at < start_at:
                continue
            if end_at is not None and at > end_at:
                continue
            if level and item.get("level") != level:
                continue
            if source and item.get("source") != source:
                continue
            if module and module.lower() not in str(item.get("module") or "").lower():
                continue
            if normalized_event and str(item.get("event") or "").strip().lower() != normalized_event:
                continue
            if normalized_symbol:
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                item_symbol = str(details.get("symbol") or details.get("name") or "").strip().upper()
                if item_symbol != normalized_symbol:
                    continue
            items.append(item)
    items.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    recent = items[: max(1, min(limit, 500))]
    last_hour_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    last_hour = []
    for item in items:
        try:
            at = datetime.fromisoformat(str(item.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at >= last_hour_cutoff:
            last_hour.append(item)
    return {
        "status": "ok",
        "count": len(recent),
        "summary": {
            "lastHourCount": len(last_hour),
            "lastHourErrors": sum(item.get("level") == "error" for item in last_hour),
            "lastErrorAt": next((item.get("at") for item in items if item.get("level") == "error"), None),
            "lastError": next((item.get("message") for item in items if item.get("level") == "error"), None),
            "retentionDays": _retention_days,
        },
        "items": recent,
    }


class FrontendRuntimeEvent(BaseModel):
    event: str = Field(min_length=1, max_length=100)
    level: str = Field(default="error", max_length=20)
    message: str = Field(min_length=1, max_length=_max_text_length)
    page: str | None = Field(default=None, max_length=500)
    module: str | None = Field(default=None, max_length=150)
    errorType: str | None = Field(default=None, max_length=200)
    stack: str | None = Field(default=None, max_length=_max_text_length)
    details: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def get_system_runtime_logs(
    limit: int = Query(default=100, ge=1, le=500),
    level: str | None = Query(default=None),
    source: str | None = Query(default=None),
    module: str | None = Query(default=None),
    event: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    start_at: datetime | None = Query(default=None, alias="startAt"),
    end_at: datetime | None = Query(default=None, alias="endAt"),
) -> dict[str, Any]:
    return system_runtime_logs_overview(
        limit,
        level=level,
        source=source,
        module=module,
        event=event,
        symbol=symbol,
        start_at=start_at,
        end_at=end_at,
    )


@router.post("/frontend", status_code=202)
def report_frontend_runtime_event(data: FrontendRuntimeEvent) -> dict[str, str]:
    append_system_runtime_event(
        data.event,
        level=data.level,
        source="frontend",
        module=data.module,
        message=data.message,
        path=data.page,
        error_type=data.errorType,
        details={**data.details, "stack": data.stack},
    )
    return {"status": "accepted"}

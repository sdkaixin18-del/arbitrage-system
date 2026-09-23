from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.database import get_data_root


router = APIRouter(prefix="/five-year-validation", tags=["expectation-gap-backtest"])
BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "run_expectation_gap_backtest.py"


def artifact_root() -> Path:
    return get_data_root() / "research" / "expectation_gap_backtest"


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _pid_running(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _status_is_live(status: dict[str, Any]) -> bool:
    if status.get("status") != "running":
        return False
    if _pid_running(status.get("pid")):
        return True
    updated_at = status.get("updated_at")
    try:
        updated = datetime.fromisoformat(str(updated_at))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated).total_seconds() < 120
    except (TypeError, ValueError):
        return False


def validation_overview() -> dict[str, Any]:
    root = artifact_root()
    status = _read_json(root / "status.json", {"status": "not_started", "message": "还没有运行五年验证"})
    summary = _read_json(root / "summary.json", None)
    manifest = _read_json(root / "source_manifest.json", None)
    return {
        "status": status,
        "summary": summary,
        "source_manifest": manifest,
        "artifacts": {
            "root": str(root),
            "events_csv": str(root / "events.csv") if (root / "events.csv").exists() else None,
            "notebook": str(root / "expectation_gap_validation.ipynb") if (root / "expectation_gap_validation.ipynb").exists() else None,
            "methodology": str(root / "methodology.md") if (root / "methodology.md").exists() else None,
        },
    }


def read_events(
    market: Literal["A股", "美股"] | None,
    decision_code: Literal["A", "B", "C", "D"] | None,
    partition: Literal["development", "validation"] | None,
    limit: int,
) -> list[dict[str, Any]]:
    path = artifact_root() / "events.csv"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if market and row.get("market") != market:
                continue
            if decision_code and row.get("decision_code") != decision_code:
                continue
            if partition and row.get("sample_partition") != partition:
                continue
            rows.append(row)
    rows.sort(key=lambda row: (row.get("signal_date") or "", row.get("market") or "", row.get("symbol") or ""), reverse=True)
    return rows[:limit]


@router.get("")
def five_year_validation_overview():
    return validation_overview()


@router.get("/events")
def five_year_validation_events(
    market: Literal["A股", "美股"] | None = None,
    decision_code: Literal["A", "B", "C", "D"] | None = None,
    partition: Literal["development", "validation"] | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
):
    return {"status": "ok", "items": read_events(market, decision_code, partition, limit)}


@router.post("/refresh")
def refresh_five_year_validation():
    root = artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    status = _read_json(root / "status.json", {})
    if _status_is_live(status):
        return {"status": "already_running", "run": status}
    if not SCRIPT_PATH.exists():
        raise HTTPException(status_code=500, detail="五年验证脚本不存在")
    log_path = root / "run.log"
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--a-symbols", "1200",
                "--us-symbols", "120",
                "--workers", "8",
                "--minimum-samples", "30",
            ],
            cwd=str(BACKEND_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log_handle.close()
        raise HTTPException(status_code=500, detail=f"启动五年验证失败：{exc}") from exc
    log_handle.close()
    return {
        "status": "started",
        "run": {
            "status": "running",
            "pid": process.pid,
            "message": "已在后台更新A股和美股五年验证",
            "log_path": str(log_path),
        },
    }

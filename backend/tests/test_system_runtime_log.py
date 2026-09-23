from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.system_runtime_log import (
    append_system_runtime_event,
    record_exception,
    system_runtime_logs_overview,
)


def test_system_runtime_log_records_source_and_redacts_sensitive_details() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "events.jsonl"
        append_system_runtime_event(
            "http_response_error",
            level="error",
            source="backend",
            module="http",
            message="GET /api/example returned 500",
            path="/api/example",
            method="GET",
            status_code=500,
            details={"query": "page=1", "token": "must-not-be-written"},
            target_path=path,
        )

        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["source"] == "backend"
        assert stored["module"] == "http"
        assert stored["statusCode"] == 500
        assert stored["details"] == {"query": "page=1"}


def test_nested_evidence_preserves_false_zero_and_number_types(tmp_path):
    path = tmp_path / "events.jsonl"
    append_system_runtime_event("astro_test", details={"report": {"checks": [
        {"unverifiedCardAllowed": False, "fee": 0, "spreadPct": 1.2, "missing": None},
    ]}}, target_path=path)
    check = json.loads(path.read_text())["details"]["report"]["checks"][0]
    assert check == {"unverifiedCardAllowed": False, "fee": 0, "spreadPct": 1.2, "missing": None}


def test_system_runtime_log_filters_and_includes_exception_traceback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "events.jsonl"
        append_system_runtime_event(
            "browser_uncaught_error",
            source="frontend",
            module="fs/pair-spread/kstr",
            message="chart failed",
            target_path=path,
        )
        try:
            raise RuntimeError("background failed")
        except RuntimeError as exc:
            record_exception(
                exc,
                event="background_thread_exception",
                module="scheduler",
                path="/api/job",
                target_path=path,
            )

        frontend = system_runtime_logs_overview(20, source="frontend", path=path)
        scheduler = system_runtime_logs_overview(20, module="sched", path=path)

        assert frontend["count"] == 1
        assert frontend["items"][0]["path"] is None
        assert scheduler["count"] == 1
        assert scheduler["items"][0]["errorType"] == "RuntimeError"
        assert "background failed" in scheduler["items"][0]["details"]["traceback"]


def test_system_runtime_log_filters_rotated_history_by_symbol_event_and_time() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "events.jsonl"
        now = datetime.now(timezone.utc)
        first = append_system_runtime_event(
            "astro_auto_card_decision_audit",
            source="backend",
            module="astro_decision_audit",
            message="ZKC initial quote",
            details={"symbol": "ZKC"},
            target_path=path,
        )
        append_system_runtime_event(
            "astro_auto_card_decision_audit",
            source="backend",
            module="astro_decision_audit",
            message="SKR initial quote",
            details={"symbol": "SKR"},
            target_path=path,
        )
        path.replace(path.with_name("events.jsonl.1"))
        append_system_runtime_event(
            "astro_card_created",
            source="backend",
            module="astro_sdk",
            message="ZKC created",
            details={"symbol": "ZKC"},
            target_path=path,
        )

        result = system_runtime_logs_overview(
            20,
            symbol="zkc",
            event="astro_auto_card_decision_audit",
            start_at=now - timedelta(seconds=1),
            end_at=now + timedelta(seconds=1),
            path=path,
        )

        assert result["count"] == 1
        assert result["items"][0]["id"] == first["id"]

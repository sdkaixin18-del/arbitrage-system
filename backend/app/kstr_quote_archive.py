from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any


_archive_lock = threading.Lock()
ARCHIVE_INTERVAL_SECONDS = 10
_last_bucket_by_code: dict[str, datetime] = {}


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _beta_asof(payload: dict[str, Any]) -> datetime | None:
    structural = payload.get("structuralModel")
    history_end = structural.get("historyEnd") if isinstance(structural, dict) else None
    if not history_end:
        return None
    parsed_date = date.fromisoformat(str(history_end))
    return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)


def archive_path() -> Path:
    from app.database import get_data_dir

    return get_data_dir() / "kstr-walkforward" / "quotes.jsonl"


def archive_kstr_quote_payload(
    payload: dict[str, Any],
    etf_code: str,
    *,
    path: Path | None = None,
) -> bool:
    normalized_code = str(etf_code).upper().removesuffix(".SH")
    if normalized_code != "588000":
        return False
    kstr = payload.get("kstr")
    kstr = kstr if isinstance(kstr, dict) else {}
    contract_exchange = str(kstr.get("exchange") or "bn").lower()
    if contract_exchange != "bn":
        return False

    timestamp = _utc_datetime(payload["updatedAt"])
    bucket = timestamp.replace(
        second=(timestamp.second // ARCHIVE_INTERVAL_SECONDS)
        * ARCHIVE_INTERVAL_SECONDS,
        microsecond=0,
    )
    freshness = payload.get("quoteFreshness")
    freshness = freshness if isinstance(freshness, dict) else {}
    threshold = float(freshness.get("thresholdSeconds") or 45)
    a_etf = payload.get("aEtf")
    a_etf = a_etf if isinstance(a_etf, dict) else {}
    fx = payload.get("fx")
    fx = fx if isinstance(fx, dict) else {}
    beta_asof = _beta_asof(payload)
    row = {
        "timestamp": timestamp.isoformat(),
        "contractExchange": contract_exchange,
        "aBid": a_etf.get("bid"),
        "aAsk": a_etf.get("ask"),
        "kstrBid": kstr.get("bid"),
        "kstrAsk": kstr.get("ask"),
        "usdCny": fx.get("usdCny"),
        "aMarketPhase": payload.get("aMarketPhase") or "unknown",
        "aQuoteFresh": bool(
            freshness.get("aEtfAgeSeconds") is not None
            and float(freshness["aEtfAgeSeconds"]) <= threshold
        ),
        "kstrQuoteFresh": bool(
            freshness.get("kstrAgeSeconds") is not None
            and float(freshness["kstrAgeSeconds"]) <= threshold
        ),
        "dynamicBeta": payload.get("hedgeBeta"),
        "dynamicBetaAsOf": beta_asof.isoformat() if beta_asof else None,
    }
    target = path or archive_path()
    with _archive_lock:
        if _last_bucket_by_code.get(normalized_code) == bucket:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        _last_bucket_by_code[normalized_code] = bucket
    return True


def try_archive_kstr_quote_payload(
    payload: dict[str, Any],
    etf_code: str,
) -> bool:
    try:
        return archive_kstr_quote_payload(payload, etf_code)
    except (OSError, TypeError, ValueError, KeyError):
        return False

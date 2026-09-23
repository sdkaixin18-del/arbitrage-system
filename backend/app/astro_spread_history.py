from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_lock = threading.Lock()
_loaded_path: Path | None = None
_history: dict[str, deque[dict[str, float | str]]] = defaultdict(deque)
_last_sample_ms: dict[str, int] = {}
_last_compact_ms = 0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float, maximum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def structure_filter_enabled() -> bool:
    return _env_bool("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", True)


def structure_history_hours() -> float:
    return _env_float("ASTRO_SPREAD_STRUCTURE_HISTORY_HOURS", 72.0, 1.0, 168.0)


def structure_tracking_min_pct() -> float:
    return _env_float("ASTRO_SPREAD_STRUCTURE_TRACKING_MIN_PCT", 0.5, 0.0, 10.0)


def structure_sample_seconds() -> float:
    return _env_float("ASTRO_SPREAD_STRUCTURE_SAMPLE_SECONDS", 60.0, 30.0, 300.0)


def structure_adverse_share() -> float:
    return _env_float("ASTRO_SPREAD_STRUCTURE_ADVERSE_SHARE", 0.75, 0.5, 1.0)


def structure_max_five_minute_increase_pct() -> float:
    return _env_float("ASTRO_SPREAD_STRUCTURE_MAX_5M_INCREASE_PCT", 0.30, 0.05, 5.0)


def _history_path() -> Path | None:
    explicit = os.environ.get("ASTRO_SPREAD_STRUCTURE_HISTORY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR", "").strip()
    return Path(data_dir).expanduser() / "astro-spread-structure-history.jsonl" if data_dir else None


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _utc_iso(epoch_ms: int | float | None) -> str | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc).isoformat()


def _prune_locked(cutoff_ms: int) -> None:
    for key in list(_history):
        points = _history[key]
        while points and int(points[0]["at"]) < cutoff_ms:
            points.popleft()
        if not points:
            _history.pop(key, None)
            _last_sample_ms.pop(key, None)


def _load_locked(path: Path, now_ms: int) -> None:
    global _loaded_path
    if _loaded_path == path:
        return
    _history.clear()
    _last_sample_ms.clear()
    cutoff_ms = now_ms - int(structure_history_hours() * 3_600_000)
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    point = json.loads(line)
                    key = str(point.get("key") or "")
                    at = int(point.get("at") or 0)
                    spread = _finite(point.get("openSpreadPct"))
                    funding = _finite(point.get("netFundingRatePct"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not key or at < cutoff_ms or spread is None:
                    continue
                normalized: dict[str, float | str] = {
                    "key": key,
                    "at": float(at),
                    "openSpreadPct": spread,
                }
                if funding is not None:
                    normalized["netFundingRatePct"] = funding
                fingerprint = str(point.get("indexFingerprint") or "").strip()
                if fingerprint:
                    normalized["indexFingerprint"] = fingerprint
                _history[key].append(normalized)
                _last_sample_ms[key] = max(_last_sample_ms.get(key, 0), at)
        except OSError:
            pass
    _loaded_path = path


def _compact_locked(path: Path, now_ms: int) -> None:
    global _last_compact_ms
    if now_ms - _last_compact_ms < 6 * 3_600_000:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for points in _history.values():
                for point in points:
                    handle.write(json.dumps(point, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(path)
        _last_compact_ms = now_ms
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def record_ff_structure_history(candidates: Iterable[dict[str, Any]], now_ms: int | None = None) -> int:
    """Persist minute samples only for direct FF routes near the entry threshold."""
    path = _history_path()
    if path is None:
        return 0
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    sample_ms = int(structure_sample_seconds() * 1000)
    tracking_min = structure_tracking_min_pct()
    pending: list[dict[str, float | str]] = []
    with _lock:
        _load_locked(path, current_ms)
        cutoff_ms = current_ms - int(structure_history_hours() * 3_600_000)
        _prune_locked(cutoff_ms)
        for candidate in candidates:
            if str(candidate.get("type") or "").upper() != "FF":
                continue
            key = str(candidate.get("key") or "")
            spread = _finite(candidate.get("openSpreadPct"))
            funding = _finite(candidate.get("netFundingRatePct"))
            if not key or spread is None or spread < tracking_min:
                continue
            if current_ms - _last_sample_ms.get(key, 0) < sample_ms:
                continue
            point: dict[str, float | str] = {
                "key": key,
                "at": float(current_ms),
                "openSpreadPct": spread,
            }
            if funding is not None:
                point["netFundingRatePct"] = funding
            fingerprint = str(candidate.get("indexFingerprint") or "").strip()
            if fingerprint:
                point["indexFingerprint"] = fingerprint
            _history[key].append(point)
            _last_sample_ms[key] = current_ms
            pending.append(point)
        if pending:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    for point in pending:
                        handle.write(json.dumps(point, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError:
                pass
        _compact_locked(path, current_ms)
    return len(pending)


def _median_absolute_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def _max_five_minute_increase(points: list[dict[str, float | str]]) -> float:
    maximum = 0.0
    left = 0
    for right, point in enumerate(points):
        at = int(point["at"])
        while left < right and at - int(points[left]["at"]) > 5 * 60_000:
            left += 1
        window_start = min(float(item["openSpreadPct"]) for item in points[left : right + 1])
        maximum = max(maximum, float(point["openSpreadPct"]) - window_start)
    return maximum


def assess_ff_structure_history(
    candidate: dict[str, Any],
    points: Iterable[dict[str, float | str]],
    *,
    now_ms: int | None = None,
    history_hours: float | None = None,
    sample_seconds: float | None = None,
    adverse_share_threshold: float | None = None,
    index_change_detected: bool = False,
) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    required_hours = structure_history_hours() if history_hours is None else float(history_hours)
    cadence_seconds = structure_sample_seconds() if sample_seconds is None else float(sample_seconds)
    adverse_threshold = structure_adverse_share() if adverse_share_threshold is None else float(adverse_share_threshold)
    cutoff_ms = current_ms - int(required_hours * 3_600_000)
    values = sorted(
        (
            point
            for point in points
            if int(point.get("at") or 0) >= cutoff_ms
            and _finite(point.get("openSpreadPct")) is not None
        ),
        key=lambda point: int(point["at"]),
    )
    current_spread = _finite(candidate.get("openSpreadPct"))
    expected_samples = max(1, int(required_hours * 3600 / max(1.0, cadence_seconds)))
    minimum_samples = max(3, math.ceil(expected_samples * 0.60))
    span_hours = (
        (int(values[-1]["at"]) - int(values[0]["at"])) / 3_600_000
        if len(values) >= 2
        else 0.0
    )
    base = {
        "enabled": True,
        "historyReady": False,
        "autoCardEligible": False,
        "classification": "warming_up",
        "reason": "结构性价差历史不足，禁止自动建卡。",
        "sampleCount": len(values),
        "minimumSamples": minimum_samples,
        "historySpanHours": round(span_hours, 2),
        "requiredHistoryHours": required_hours,
        "oldestAt": _utc_iso(int(values[0]["at"])) if values else None,
        "indexRegime": "changed" if index_change_detected else "no_detected_change",
    }
    if current_spread is None or len(values) < minimum_samples or span_hours < required_hours * 0.90:
        return base

    funding = [
        float(value)
        for point in values
        if (value := _finite(point.get("netFundingRatePct"))) is not None
    ]
    funding_coverage = len(funding) / len(values)
    if funding_coverage < 0.75:
        return {
            **base,
            "historyReady": True,
            "classification": "funding_missing",
            "reason": "净资金费历史覆盖不足，禁止自动建卡。",
            "fundingCoverage": round(funding_coverage, 4),
        }

    fingerprints = [str(point.get("indexFingerprint") or "").strip() for point in values]
    fingerprints = [fingerprint for fingerprint in fingerprints if fingerprint]
    index_coverage = len(fingerprints) / len(values)
    if candidate.get("indexEvidenceRequired") and index_coverage < 0.75:
        return {
            **base,
            "historyReady": True,
            "classification": "index_history_missing",
            "reason": "Gate 指数成分历史覆盖不足，禁止自动建卡。",
            "fundingCoverage": round(funding_coverage, 4),
            "indexCoverage": round(index_coverage, 4),
        }
    index_change_detected = index_change_detected or len(set(fingerprints)) > 1
    base["indexRegime"] = "changed" if index_change_detected else "stable"

    spreads = [float(point["openSpreadPct"]) for point in values]
    median_spread = statistics.median(spreads)
    spread_mad = _median_absolute_deviation(spreads)
    robust_upper = median_spread + max(0.30, 2.5 * 1.4826 * spread_mad)
    max_five_minute = _max_five_minute_increase(values)
    one_minute_changes = [spreads[index] - spreads[index - 1] for index in range(1, len(spreads))]
    change_mad = _median_absolute_deviation(one_minute_changes)
    sudden_limit = max(structure_max_five_minute_increase_pct(), 3 * 1.4826 * change_mad)
    adverse_share = sum(value < 0 for value in funding) / len(funding)
    persistent_adverse = adverse_share >= adverse_threshold
    gradual = max_five_minute <= sudden_limit
    historically_normal = current_spread <= robust_upper
    structural = persistent_adverse and gradual and historically_normal and not index_change_detected

    metrics = {
        **base,
        "historyReady": True,
        "fundingCoverage": round(funding_coverage, 4),
        "indexCoverage": round(index_coverage, 4),
        "adverseFundingShare": round(adverse_share, 4),
        "adverseFundingThreshold": adverse_threshold,
        "medianSpreadPct": round(median_spread, 6),
        "spreadMadPct": round(spread_mad, 6),
        "normalUpperPct": round(robust_upper, 6),
        "maxFiveMinuteIncreasePct": round(max_five_minute, 6),
        "suddenIncreaseLimitPct": round(sudden_limit, 6),
        "persistentAdverseFunding": persistent_adverse,
        "gradualFormation": gradual,
        "withinHistoricalBand": historically_normal,
    }
    if structural:
        return {
            **metrics,
            "autoCardEligible": False,
            "classification": "structural",
            "reason": "资金费持续亏损且价差缓慢形成，属于结构性合理价差。",
        }
    if index_change_detected:
        reason = "检测到指数成分或权重变化，保留为异常建卡候选。"
    elif not gradual:
        reason = "价差在 5 分钟内异常扩大，保留为建卡候选。"
    elif not historically_normal:
        reason = "价差突破历史稳健区间，保留为建卡候选。"
    else:
        reason = "资金费未持续拖累当前方向，保留为建卡候选。"
    return {
        **metrics,
        "autoCardEligible": True,
        "classification": "abnormal",
        "reason": reason,
    }


def ff_structure_assessment(
    candidate: dict[str, Any],
    *,
    now_ms: int | None = None,
    index_change_detected: bool = False,
) -> dict[str, Any]:
    if not structure_filter_enabled():
        return {
            "enabled": False,
            "historyReady": True,
            "autoCardEligible": True,
            "classification": "disabled",
            "reason": "结构性价差过滤已关闭。",
        }
    path = _history_path()
    if path is None:
        return {
            "enabled": True,
            "historyReady": False,
            "autoCardEligible": False,
            "classification": "storage_missing",
            "reason": "未配置结构性价差历史目录，禁止自动建卡。",
        }
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with _lock:
        _load_locked(path, current_ms)
        points = list(_history.get(str(candidate.get("key") or ""), ()))
    return assess_ff_structure_history(
        candidate,
        points,
        now_ms=current_ms,
        index_change_detected=index_change_detected,
    )


def ff_structure_history_status(now_ms: int | None = None) -> dict[str, Any]:
    path = _history_path()
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if path is None:
        return {"path": None, "routeCount": 0, "sampleCount": 0, "oldestAt": None}
    with _lock:
        _load_locked(path, current_ms)
        points = [point for route in _history.values() for point in route]
    oldest = min((int(point["at"]) for point in points), default=None)
    return {
        "path": str(path),
        "routeCount": len(_history),
        "sampleCount": len(points),
        "oldestAt": _utc_iso(oldest),
    }


def reset_ff_structure_history_for_tests() -> None:
    global _loaded_path, _last_compact_ms
    with _lock:
        _history.clear()
        _last_sample_ms.clear()
        _loaded_path = None
        _last_compact_ms = 0

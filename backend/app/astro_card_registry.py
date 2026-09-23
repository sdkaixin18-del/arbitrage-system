from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_registry_lock = threading.Lock()
_memory_registry: dict[str, Any] = {"version": 1, "routes": {}}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _registry_path() -> Path | None:
    explicit = os.environ.get("ASTRO_AUTO_CARD_REGISTRY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR", "").strip()
    return Path(data_dir).expanduser() / "astro-auto-card-registry.json" if data_dir else None


def _subscription_path() -> Path | None:
    explicit = os.environ.get("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR", "").strip()
    return Path(data_dir).expanduser() / "astro-spread-subscriptions.json" if data_dir else None


def _read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def astro_delete_rearm_pct() -> float:
    saved = _read_json(_subscription_path()).get("deleteRearmPct")
    raw = saved if saved is not None else os.environ.get("ASTRO_AUTO_CARD_DELETE_REARM_PCT", "20")
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = 20.0
    if not math.isfinite(parsed):
        parsed = 20.0
    return max(0.0, min(parsed, 1000.0))


def astro_delete_pullback_pct_points() -> float:
    saved = _read_json(_subscription_path()).get("deletePullbackPctPoints")
    raw = saved if saved is not None else os.environ.get(
        "ASTRO_AUTO_CARD_DELETE_PULLBACK_PCT_POINTS", "0.5"
    )
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = 0.5
    if not math.isfinite(parsed):
        parsed = 0.5
    return max(0.0, min(parsed, 100.0))


def astro_delete_confirmation_seconds() -> float:
    try:
        parsed = float(os.environ.get("ASTRO_AUTO_CARD_DELETE_CONFIRM_SECONDS", "5"))
    except (TypeError, ValueError):
        parsed = 5.0
    if not math.isfinite(parsed):
        parsed = 5.0
    return max(1.0, min(parsed, 60.0))


def _bounded_seconds(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(parsed, maximum))


def astro_cleanup_grace_seconds() -> float:
    return _bounded_seconds("ASTRO_AUTO_CARD_CLEANUP_GRACE_SECONDS", 120.0, 30.0, 3600.0)


def astro_cleanup_invalid_seconds() -> float:
    return _bounded_seconds("ASTRO_AUTO_CARD_CLEANUP_INVALID_SECONDS", 60.0, 10.0, 3600.0)


def astro_cleanup_cooldown_seconds() -> float:
    return _bounded_seconds("ASTRO_AUTO_CARD_CLEANUP_COOLDOWN_SECONDS", 30.0, 10.0, 86400.0)


def astro_cleanup_rearm_buffer_pct_points() -> float:
    return _bounded_seconds("ASTRO_AUTO_CARD_CLEANUP_REARM_BUFFER_PCT_POINTS", 0.1, 0.0, 10.0)


RouteIdentity = tuple[str, str, str, str]
LifecycleIdentity = tuple[str, ...]


def pair_identity(pair: dict[str, Any]) -> RouteIdentity:
    return (
        str(pair.get("name") or "").strip().upper(),
        str(pair.get("type") or "").strip().upper(),
        str(pair.get("buyEx") or "").strip().lower(),
        str(pair.get("sellEx") or "").strip().lower(),
    )


def _normalized_dex_address(value: Any) -> str:
    address = str(value or "").strip()
    return address.lower() if address.lower().startswith("0x") else address


def _dex_identity_parts(pair: dict[str, Any]) -> tuple[str, str]:
    dex_config = pair.get("_dexConfig") if isinstance(pair.get("_dexConfig"), dict) else {}
    chain_index = str(
        dex_config.get("chainIndex")
        or pair.get("dexChainIndex")
        or ""
    ).strip()
    contract_address = _normalized_dex_address(
        dex_config.get("contractAddress") or pair.get("dexContractAddress")
    )
    return chain_index, contract_address


def pair_lifecycle_identity(pair: dict[str, Any]) -> LifecycleIdentity:
    """Return the local lifecycle key, including an exact DEX asset fingerprint.

    Astro's pair list does not expose chain/contract metadata, so API-level
    duplicate checks still use :func:`pair_identity`.  The local registry must
    nevertheless distinguish two assets that share a ticker and route.
    """

    route = pair_identity(pair)
    chain_index, contract_address = _dex_identity_parts(pair)
    if "okxdex" not in route[2:] or not chain_index or not contract_address:
        return route
    return (*route, chain_index, contract_address)


def _identity_key(identity: LifecycleIdentity) -> str:
    return "|".join(identity)


def _record_matches_route(record: dict[str, Any], route: RouteIdentity) -> bool:
    return pair_identity(record) == route


def _find_record(
    routes: dict[str, Any],
    pair_or_identity: dict[str, Any] | RouteIdentity,
) -> dict[str, Any] | None:
    if isinstance(pair_or_identity, dict):
        exact = routes.get(_identity_key(pair_lifecycle_identity(pair_or_identity)))
        if isinstance(exact, dict):
            return exact
        route = pair_identity(pair_or_identity)
    else:
        route = pair_or_identity
        exact = routes.get(_identity_key(route))
        if isinstance(exact, dict):
            return exact
    matches = [record for record in routes.values() if isinstance(record, dict) and _record_matches_route(record, route)]
    if not matches:
        return None
    # Remote Astro rows cannot reveal a DEX fingerprint.  Prefer the most
    # recently observed local record when resolving such a row.
    matches.sort(key=lambda record: str(record.get("lastSeenAt") or record.get("createdAt") or ""), reverse=True)
    return matches[0]


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "routes": {}, "pendingSubmissions": {}}


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    if path is None:
        payload = dict(_memory_registry)
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = _empty_registry()
        except (OSError, ValueError) as exc:
            raise RuntimeError("Astro 自动卡注册表读取失败，暂停建卡与清理") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Astro 自动卡注册表格式异常，暂停建卡与清理")
    routes = payload.get("routes")
    pending = payload.get("pendingSubmissions")
    if (routes is not None and not isinstance(routes, dict)) or (pending is not None and not isinstance(pending, dict)):
        raise RuntimeError("Astro 自动卡注册表记录异常，暂停建卡与清理")
    for record in (pending or {}).values():
        if not isinstance(record, dict) or not isinstance(record.get("pair"), dict) or not all(pair_identity(record["pair"])):
            raise RuntimeError("Astro 待确认提交记录异常，暂停建卡与清理")
    return {"version": 1, "routes": routes if isinstance(routes, dict) else {}, "pendingSubmissions": pending if isinstance(pending, dict) else {},
            "submissionHistory": payload.get("submissionHistory", []) if isinstance(payload.get("submissionHistory"), list) else []}


def _save_registry(payload: dict[str, Any]) -> None:
    global _memory_registry
    path = _registry_path()
    if path is None:
        _memory_registry = payload
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


AUTO_CARD_CONFIG_FIELDS = (
    "openPosition",
    "closePosition",
    "disableOpen",
    "disableClose",
    "maxTradeUSDT",
    "leverage",
    "minNotional",
    "maxNotional",
    "startTime",
    "greaterPriceAlert",
    "lessPriceAlert",
    "priceAlert",
    "priceAlertOnlyRise",
    "spotMarginType",
    "adjustParams",
    "boostMode",
    "slowMode",
    "stepOpen",
    "stepClose",
    "stopLoss",
)
AUTO_CARD_SNAPSHOT_VERSION = 2
_SNAPSHOT_FIELDS = ("name", "type", "buyEx", "sellEx", *AUTO_CARD_CONFIG_FIELDS)


def inactive_unreadable_config_fields(snapshot: dict[str, Any], actual: dict[str, Any]) -> set[str]:
    # The SDK uses an empty priceAlert to disable price-change notifications.
    # Its direction selector is behaviorally inactive only when both observed
    # configurations explicitly disable that alert. Missing is not False.
    if snapshot.get("priceAlert") == "" and actual.get("priceAlert") == "" and "priceAlertOnlyRise" not in actual:
        return {"priceAlertOnlyRise"}
    return set()


def _pair_snapshot(pair: dict[str, Any]) -> dict[str, Any]:
    # Detach nested editable settings from the mutable SDK response.
    snapshot = json.loads(json.dumps({field: pair.get(field) for field in _SNAPSHOT_FIELDS if field in pair}))
    chain_index, contract_address = _dex_identity_parts(pair)
    if chain_index and contract_address:
        snapshot["dexChainIndex"] = chain_index
        snapshot["dexContractAddress"] = contract_address
    if isinstance(pair.get("_announcementCard"), dict):
        snapshot["_announcementCard"] = json.loads(json.dumps(pair["_announcementCard"]))
    return snapshot


def _route_record(
    pair: dict[str, Any],
    now: datetime,
    *,
    astro_pair: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name, pair_type, buy_exchange, sell_exchange = pair_identity(pair)
    chain_index, contract_address = _dex_identity_parts(pair)
    verified = astro_pair if isinstance(astro_pair, dict) else pair
    return {
        "name": name,
        "type": pair_type,
        "buyEx": buy_exchange,
        "sellEx": sell_exchange,
        "createdAt": _iso(now),
        "announcementPrecreated": isinstance(pair.get("_announcementCard"), dict),
        "lastSeenAt": _iso(now),
        "missingObservedAt": None,
        "missingReferenceOpenPosition": None,
        "deletionDetectedAt": None,
        "deletionReferenceOpenPosition": None,
        "rearmPullbackObservedAt": None,
        "rearmPullbackConfirmedAt": None,
        "rearmPullbackPctPoints": None,
        "createdPair": _pair_snapshot({**pair, **verified}),
        "createdPairSnapshotVersion": AUTO_CARD_SNAPSHOT_VERSION,
        "createdReadbackConfigFields": [field for field in AUTO_CARD_CONFIG_FIELDS if field in verified],
        "submittedOnlyConfigFields": [field for field in AUTO_CARD_CONFIG_FIELDS if field in pair and field not in verified],
        "inactiveUnreadableConfigFields": sorted(inactive_unreadable_config_fields(pair, verified)),
        "astroPairId": verified.get("id"),
        "dexChainIndex": chain_index or None,
        "dexContractAddress": contract_address or None,
        "invalidObservedAt": None,
        "invalidReason": None,
        "systemDeletedAt": None,
        "systemDeleteCooldownUntil": None,
        "systemDeleteReason": None,
    }


def register_auto_created_pair(
    pair: dict[str, Any],
    *,
    astro_pair: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    resolved_now = now or _utc_now()
    route = pair_identity(pair)
    identity = pair_lifecycle_identity(pair)
    if not all(route):
        return
    with _registry_lock:
        payload = _load_registry()
        payload["routes"][_identity_key(identity)] = _route_record(
            pair,
            resolved_now,
            astro_pair=astro_pair,
        )
        pending = payload["pendingSubmissions"].pop(_identity_key(identity), None)
        if pending:
            _archive_submission(payload, pending, "confirmed", "已回读确认卡片", (astro_pair or {}).get("id"))
        _save_registry(payload)


def record_pending_astro_submission(pair: dict[str, Any], state: str, error: str | None = None, **metadata) -> None:
    with _registry_lock:
        payload = _load_registry()
        key = _identity_key(pair_lifecycle_identity(pair))
        previous = payload["pendingSubmissions"].get(key) or {}
        payload["pendingSubmissions"][key] = {
            **previous, **metadata,
            "pair": _pair_snapshot(pair), "state": state,
            "submissionId": previous.get("submissionId") or str(uuid.uuid4()),
            "submittedAt": previous.get("submittedAt") or _iso(_utc_now()),
            "updatedAt": _iso(_utc_now()), "error": error,
        }
        _save_registry(payload)


SUBMISSION_CHECK_SECONDS = (2, 5, 15, 30, 60)


def _archive_submission(payload, record, state, resolution, card_id=None):
    history = payload.setdefault("submissionHistory", [])
    history.append({**record, "state": state, "resolution": resolution,
                    "submissionId": record.get("submissionId") or str(uuid.uuid4()),
                    "cardId": card_id, "resolvedAt": _iso(_utc_now())})
    payload["submissionHistory"] = history[-100:]


def record_submission_request(pair: dict[str, Any], nonce: str, timestamp: int) -> None:
    with _registry_lock:
        payload = _load_registry()
        record = payload["pendingSubmissions"].get(_identity_key(pair_lifecycle_identity(pair)))
        if record:
            record.update(requestNonce=nonce, requestTimestamp=timestamp)
            _save_registry(payload)


def pending_submission_checks_due() -> list[dict[str, Any]]:
    """Bounded polling, persisted across restarts; never infer failure from absence."""
    due = []
    with _registry_lock:
        payload = _load_registry()
        changed = False
        for key, record in payload["pendingSubmissions"].items():
            if not record.get("submissionId"):
                record["submissionId"] = str(uuid.uuid4())
                changed = True
            if record.get("state") == "needs_review":
                continue
            submitted = _parse_iso(record.get("submittedAt"))
            age = (_utc_now() - submitted).total_seconds() if submitted else 61
            count = int(record.get("checkScheduleIndex") or 0)
            if age > 65 or count >= len(SUBMISSION_CHECK_SECONDS):
                record.update(state="needs_review", updatedAt=_iso(_utc_now()),
                              reviewReason="核对期限已结束，提交结果仍未知；保留防重复锁")
                changed = True
            elif age >= SUBMISSION_CHECK_SECONDS[count]:
                # Missed intervals after restart are skipped, not replayed in a burst.
                index = max(i for i, seconds in enumerate(SUBMISSION_CHECK_SECONDS) if seconds <= age)
                due.append({"key": key, "submissionId": record["submissionId"], "checkIndex": index})
        if changed:
            _save_registry(payload)
    return due


def record_submission_check(due: list[dict[str, Any]], *, error: str | None = None) -> None:
    with _registry_lock:
        payload = _load_registry()
        for item in due:
            record = payload["pendingSubmissions"].get(item["key"])
            if not record or record.get("submissionId") != item["submissionId"]:
                continue
            record.update(checkCount=int(record.get("checkCount") or 0) + 1,
                          checkScheduleIndex=item["checkIndex"] + 1, lastCheckedAt=_iso(_utc_now()),
                          lastCheckOutcome="read_failed" if error else "not_uniquely_found",
                          lastCheckError=error, updatedAt=_iso(_utc_now()))
            if item["checkIndex"] == len(SUBMISSION_CHECK_SECONDS) - 1:
                record.update(state="needs_review", reviewReason="五次核对结束，提交结果仍未知；保留防重复锁")
        _save_registry(payload)


def mark_submission_not_executed(pair: dict[str, Any], reason: str) -> None:
    """Only for proof of no execution, never list absence or a read timeout."""
    with _registry_lock:
        payload = _load_registry()
        record = payload["pendingSubmissions"].pop(_identity_key(pair_lifecycle_identity(pair)), None)
        if record:
            _archive_submission(payload, record, "failed_not_executed", reason)
            _save_registry(payload)


def resolve_reviewed_submission(submission_id: str, evidence: str) -> dict[str, Any]:
    """Explicit operator attestation, not an inference from failed list reads."""
    if not submission_id or not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 1000:
        raise ValueError("需要提交编号和已核实未执行的证据（10–1000字）")
    with _registry_lock:
        payload = _load_registry()
        matches = [(key, r) for key, r in payload["pendingSubmissions"].items() if r.get("submissionId") == submission_id]
        if len(matches) != 1 or matches[0][1].get("state") != "needs_review":
            raise ValueError("提交状态已变化或尚在自动核对中，请刷新后核对")
        key, record = matches[0]
        _archive_submission(payload, record, "failed_not_executed", "人工核实：" + evidence.strip())
        del payload["pendingSubmissions"][key]
        _save_registry(payload)
        return {"submissionId": submission_id, "route": pair_identity(record["pair"]), "state": "failed_not_executed"}


def clear_pending_astro_submission(pair: dict[str, Any]) -> None:
    with _registry_lock:
        payload = _load_registry()
        if payload["pendingSubmissions"].pop(_identity_key(pair_lifecycle_identity(pair)), None) is not None:
            _save_registry(payload)


def pending_astro_submission_status() -> dict[str, Any]:
    with _registry_lock:
        payload = _load_registry()
        pending = list(payload["pendingSubmissions"].values())
    items = []
    for record in pending:
        if not isinstance(record, dict) or not isinstance(record.get("pair"), dict):
            continue
        pair = record["pair"]
        items.append({**{field: pair.get(field) for field in ("name", "type", "buyEx", "sellEx")}, **{field: record.get(field) for field in ("state", "submittedAt", "updatedAt", "error", "submissionId", "requestNonce", "checkCount", "lastCheckedAt", "lastCheckOutcome", "lastCheckError", "reviewReason")}})
    review_count = sum(item["state"] == "needs_review" for item in items)
    recent = [{**{field: (r.get("pair") or {}).get(field) for field in ("name", "type", "buyEx", "sellEx")},
               **{field: r.get(field) for field in ("submissionId", "state", "resolution", "resolvedAt", "cardId")}}
              for r in reversed(payload.get("submissionHistory", [])[-10:])]
    return {"count": len(items), "waitingCount": len(items) - review_count, "reviewCount": review_count,
            "items": items, "recentResolutions": recent, "checkScheduleSeconds": list(SUBMISSION_CHECK_SECONDS),
            "policy": "提交后2/5/15/30/60秒核对；到期转人工核对并停止专项查询，保留防重复锁；正常列表读取仍可补认成功"}


def reconcile_pending_astro_submissions(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved = []
    with _registry_lock:
        payload = _load_registry()
        for key, pending in list(payload["pendingSubmissions"].items()):
            pair = pending.get("pair") if isinstance(pending, dict) else None
            if not isinstance(pair, dict):
                continue
            matches = [item for item in existing if pair_identity(item) == pair_identity(pair)]
            if len(matches) != 1:
                continue
            record = _route_record(pair, _parse_iso(pending.get("submittedAt")) or _utc_now(), astro_pair=matches[0])
            # The user may have changed this card while confirmation was
            # unavailable; adopting it must never overwrite that ownership.
            record["cleanupProtectedReason"] = "submission_confirmed_after_uncertain_wait"
            payload["routes"][key] = record
            _archive_submission(payload, pending, "confirmed", "后续卡片列表补认成功", matches[0].get("id"))
            del payload["pendingSubmissions"][key]
            resolved.append({"route": pair_identity(pair), "cardId": matches[0].get("id")})
        if resolved:
            _save_registry(payload)
    return resolved


def auto_created_route_records() -> list[dict[str, Any]]:
    with _registry_lock:
        payload = _load_registry()
        return [dict(record) for record in payload["routes"].values() if isinstance(record, dict)]


def observe_auto_card_cleanup(
    observations: dict[RouteIdentity, dict[str, Any]],
    existing_pairs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    grace_seconds: float | None = None,
    invalid_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Track continuous, trustworthy rule failures for locally-created cards."""

    resolved_now = now or _utc_now()
    resolved_grace = astro_cleanup_grace_seconds() if grace_seconds is None else max(0.0, grace_seconds)
    resolved_invalid = astro_cleanup_invalid_seconds() if invalid_seconds is None else max(0.0, invalid_seconds)
    existing = {pair_identity(pair): pair for pair in existing_pairs}
    ready: list[dict[str, Any]] = []
    with _registry_lock:
        payload = _load_registry()
        changed = False
        for record in payload["routes"].values():
            if not isinstance(record, dict):
                continue
            identity = pair_identity(record)
            pair = existing.get(identity)
            observation = observations.get(identity)
            registered_id = str(record.get("astroPairId") or "")
            current_id = str((pair or {}).get("id") or "")
            if registered_id and current_id and registered_id != current_id:
                # The old auto-created ID does not grant ownership of a
                # replacement card. End its cleanup window without adopting
                # the replacement or changing the independent rearm rules.
                if record.get("cleanupSupersededById") != current_id:
                    record.update(cleanupSupersededById=current_id,
                                  cleanupSupersededAt=_iso(resolved_now),
                                  invalidObservedAt=None, invalidReason=None)
                    changed = True
                continue
            if registered_id and current_id == registered_id and record.get("cleanupSupersededById"):
                record.pop("cleanupSupersededById", None)
                record.pop("cleanupSupersededAt", None)
                changed = True
            # Announcement cards are intentionally held without executable
            # prices. Their owner decides when to start/remove them.
            if record.get("announcementPrecreated"):
                continue
            invalid_at = _parse_iso(record.get("invalidObservedAt"))

            # A missing card, an unavailable quote, or any non-invalid state
            # breaks the continuous invalidity window. Missing cards are dealt
            # with by the separate manual-delete rearm logic.
            if pair is None or not isinstance(observation, dict) or observation.get("state") != "invalid":
                if invalid_at is not None or record.get("invalidReason"):
                    record["invalidObservedAt"] = None
                    record["invalidReason"] = None
                    changed = True
                continue

            created_at = _parse_iso(record.get("createdAt"))
            if created_at is None or (resolved_now - created_at).total_seconds() < resolved_grace:
                if invalid_at is not None or record.get("invalidReason"):
                    record["invalidObservedAt"] = None
                    record["invalidReason"] = None
                    changed = True
                continue

            reason = str(observation.get("reason") or "rule_no_longer_eligible")
            if invalid_at is None:
                record["invalidObservedAt"] = _iso(resolved_now)
                record["invalidReason"] = reason
                changed = True
                continue
            if record.get("invalidReason") != reason:
                record["invalidObservedAt"] = _iso(resolved_now)
                record["invalidReason"] = reason
                changed = True
                continue
            invalid_for = (resolved_now - invalid_at).total_seconds()
            if invalid_for >= resolved_invalid:
                ready.append({
                    "record": dict(record),
                    "pair": pair,
                    "observation": dict(observation),
                    "invalidForSeconds": round(invalid_for, 3),
                })
        if changed:
            _save_registry(payload)
    return ready


def reset_auto_card_invalid_observation(identity: RouteIdentity) -> None:
    with _registry_lock:
        payload = _load_registry()
        record = _find_record(payload["routes"], identity)
        if not isinstance(record, dict):
            return
        if record.get("invalidObservedAt") is None and not record.get("invalidReason"):
            return
        record["invalidObservedAt"] = None
        record["invalidReason"] = None
        _save_registry(payload)


def mark_auto_card_system_deleted(
    identity: RouteIdentity,
    reason: str,
    *,
    now: datetime | None = None,
    cooldown_seconds: float | None = None,
) -> None:
    resolved_now = now or _utc_now()
    resolved_cooldown = (
        astro_cleanup_cooldown_seconds() if cooldown_seconds is None else max(0.0, cooldown_seconds)
    )
    with _registry_lock:
        payload = _load_registry()
        record = _find_record(payload["routes"], identity)
        if not isinstance(record, dict):
            return
        record["systemDeletedAt"] = _iso(resolved_now)
        record["systemDeleteCooldownUntil"] = _iso(
            datetime.fromtimestamp(resolved_now.timestamp() + resolved_cooldown, timezone.utc)
        )
        record["systemDeleteReason"] = reason
        record["invalidObservedAt"] = None
        record["invalidReason"] = None
        record["missingObservedAt"] = None
        record["missingReferenceOpenPosition"] = None
        record["deletionDetectedAt"] = None
        record["deletionReferenceOpenPosition"] = None
        record["rearmPullbackObservedAt"] = None
        record["rearmPullbackConfirmedAt"] = None
        record["rearmPullbackPctPoints"] = None
        _save_registry(payload)


def _open_position(pair: dict[str, Any]) -> float | None:
    try:
        value = float(pair.get("openPosition"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _record_pair(record: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(record.get("createdPair") or {}) if isinstance(record.get("createdPair"), dict) else {}
    return {
        **snapshot,
        "name": record.get("name"),
        "type": record.get("type"),
        "buyEx": record.get("buyEx"),
        "sellEx": record.get("sellEx"),
        "dexChainIndex": record.get("dexChainIndex"),
        "dexContractAddress": record.get("dexContractAddress"),
    }


def _missing_reference(record: dict[str, Any], observation: dict[str, Any] | None) -> float | None:
    if isinstance(observation, dict) and observation.get("state") in {"eligible", "invalid"}:
        try:
            open_spread_pct = float(observation.get("openSpreadPct"))
        except (TypeError, ValueError):
            open_spread_pct = float("nan")
        if math.isfinite(open_spread_pct) and open_spread_pct >= 0:
            return open_spread_pct / 100
    created_pair = record.get("createdPair")
    return _open_position(created_pair) if isinstance(created_pair, dict) else None


def apply_delete_rearm_rules(
    requested_pairs: list[dict[str, Any]],
    existing_pairs: list[dict[str, Any]],
    *,
    route_observations: dict[RouteIdentity, dict[str, Any]] | None = None,
    now: datetime | None = None,
    rearm_pct: float | None = None,
    pullback_pct_points: float | None = None,
    confirmation_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Suppress recreation until either independent rearm path is satisfied.

    A missing route must be observed on two successful Astro list responses,
    separated by the confirmation interval, before its reference is frozen.
    It can then rearm after a confirmed absolute pullback followed by a normal
    eligible signal, or directly when the spread exceeds the deletion reference
    by the configured relative percentage.
    """

    resolved_now = now or _utc_now()
    resolved_rearm_pct = astro_delete_rearm_pct() if rearm_pct is None else max(0.0, rearm_pct)
    resolved_pullback = (
        astro_delete_pullback_pct_points()
        if pullback_pct_points is None
        else max(0.0, pullback_pct_points)
    )
    resolved_confirmation = (
        astro_delete_confirmation_seconds()
        if confirmation_seconds is None
        else max(0.0, confirmation_seconds)
    )
    observations = route_observations or {}
    existing_identities = {pair_identity(pair) for pair in existing_pairs}
    allowed: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    newly_guarded: list[dict[str, Any]] = []

    with _registry_lock:
        payload = _load_registry()
        routes: dict[str, Any] = payload["routes"]
        changed = False

        # A manually restored or still-existing card cancels pending deletion
        # and any route rearm guard immediately.  Astro omits DEX fingerprints,
        # so one remote row clears every local lifecycle record for that route.
        for record in routes.values():
            if not isinstance(record, dict) or pair_identity(record) not in existing_identities:
                continue
            record["lastSeenAt"] = _iso(resolved_now)
            if (
                record.get("missingObservedAt")
                or record.get("missingReferenceOpenPosition") is not None
                or record.get("deletionDetectedAt")
                or record.get("deletionReferenceOpenPosition") is not None
                or record.get("rearmPullbackObservedAt")
                or record.get("rearmPullbackConfirmedAt")
                or record.get("systemDeletedAt")
                or record.get("systemDeleteCooldownUntil")
            ):
                record["missingObservedAt"] = None
                record["missingReferenceOpenPosition"] = None
                record["deletionDetectedAt"] = None
                record["deletionReferenceOpenPosition"] = None
                record["rearmPullbackObservedAt"] = None
                record["rearmPullbackConfirmedAt"] = None
                record["rearmPullbackPctPoints"] = None
                record["systemDeletedAt"] = None
                record["systemDeleteCooldownUntil"] = None
                record["systemDeleteReason"] = None
            changed = True

        # Detect a manual deletion independently of whether the route happens to
        # be requested in this scan.  Freeze the first trustworthy spread seen
        # after disappearance (closest available observation to deletion), not
        # the later retrigger candidate that happens to confirm it.
        requested_by_route: dict[RouteIdentity, list[dict[str, Any]]] = {}
        for requested_pair in requested_pairs:
            requested_by_route.setdefault(pair_identity(requested_pair), []).append(requested_pair)
        for record in routes.values():
            if not isinstance(record, dict):
                continue
            identity = pair_identity(record)
            if identity in existing_identities:
                continue
            if record.get("systemDeletedAt") or record.get("systemDeleteReason"):
                continue
            if record.get("deletionReferenceOpenPosition") is not None:
                continue
            observation = observations.get(identity)
            missing_at = _parse_iso(record.get("missingObservedAt"))
            if missing_at is None:
                record["missingObservedAt"] = _iso(resolved_now)
                record["missingReferenceOpenPosition"] = _missing_reference(record, observation)
                changed = True
                continue
            if (resolved_now - missing_at).total_seconds() < resolved_confirmation:
                continue
            reference_value = record.get("missingReferenceOpenPosition")
            try:
                reference_value = float(reference_value) if reference_value is not None else None
            except (TypeError, ValueError):
                reference_value = None
            if reference_value is None or not math.isfinite(reference_value) or reference_value < 0:
                reference_value = _missing_reference(record, observation)
            if reference_value is None:
                continue
            rearm_value = reference_value * (1 + resolved_rearm_pct / 100)
            record["deletionDetectedAt"] = _iso(resolved_now)
            record["deletionReferenceOpenPosition"] = reference_value
            record["missingReferenceOpenPosition"] = reference_value
            record["rearmPullbackObservedAt"] = None
            record["rearmPullbackConfirmedAt"] = None
            record["rearmPullbackPctPoints"] = None
            changed = True
            pair = (requested_by_route.get(identity) or [_record_pair(record)])[0]
            newly_guarded.append({
                "pair": pair,
                "reason": "waiting_for_pullback_or_direct_breakout",
                "deletionDetectedAt": _iso(resolved_now),
                "deletionReferenceOpenPosition": reference_value,
                "rearmOpenPosition": rearm_value,
                "pullbackRequiredPctPoints": resolved_pullback,
            })

        # Pullback and direct-breakout are independent rearm paths. Count only
        # fresh observations carrying an executable spread. Missing/unavailable
        # observations break the confirmation window.
        for record in routes.values():
            if not isinstance(record, dict) or record.get("deletionReferenceOpenPosition") is None:
                continue
            confirmed_pullback = record.get("rearmPullbackPctPoints")
            try:
                confirmed_pullback_value = (
                    float(confirmed_pullback) if confirmed_pullback is not None else None
                )
            except (TypeError, ValueError):
                confirmed_pullback_value = None
            if record.get("rearmPullbackConfirmedAt") and (
                confirmed_pullback_value is None
                or not math.isfinite(confirmed_pullback_value)
                or confirmed_pullback_value < resolved_pullback
            ):
                # Old releases armed routes merely after falling below the base
                # FF/SF threshold. That legacy flag is not proof of today's
                # absolute percentage-point pullback and must be observed again.
                record["rearmPullbackObservedAt"] = None
                record["rearmPullbackConfirmedAt"] = None
                record["rearmPullbackPctPoints"] = None
                changed = True
            if record.get("rearmPullbackConfirmedAt"):
                continue
            identity = (
                str(record.get("name") or "").strip().upper(),
                str(record.get("type") or "").strip().upper(),
                str(record.get("buyEx") or "").strip().lower(),
                str(record.get("sellEx") or "").strip().lower(),
            )
            observation = observations.get(identity)
            try:
                reference_value = float(record.get("deletionReferenceOpenPosition"))
            except (TypeError, ValueError):
                reference_value = None
            if reference_value is not None and not math.isfinite(reference_value):
                reference_value = None
            observed_open_pct = None
            if isinstance(observation, dict):
                try:
                    observed_open_pct = float(observation.get("openSpreadPct"))
                except (TypeError, ValueError):
                    observed_open_pct = None
            pullback_amount_pct_points = (
                reference_value * 100 - observed_open_pct
                if reference_value is not None
                and observed_open_pct is not None
                and math.isfinite(observed_open_pct)
                else None
            )
            is_pullback = (
                isinstance(observation, dict)
                and observation.get("state") in {"eligible", "invalid"}
                and pullback_amount_pct_points is not None
                and pullback_amount_pct_points >= resolved_pullback
            )
            observed_at = _parse_iso(record.get("rearmPullbackObservedAt"))
            if not is_pullback:
                if observed_at is not None:
                    record["rearmPullbackObservedAt"] = None
                    record["rearmPullbackPctPoints"] = None
                    changed = True
                continue
            if observed_at is None:
                record["rearmPullbackObservedAt"] = _iso(resolved_now)
                record["rearmPullbackPctPoints"] = pullback_amount_pct_points
                changed = True
                continue
            if (resolved_now - observed_at).total_seconds() >= resolved_confirmation:
                record["rearmPullbackConfirmedAt"] = _iso(resolved_now)
                record["rearmPullbackPctPoints"] = pullback_amount_pct_points
                changed = True

        for pair in requested_pairs:
            identity = pair_identity(pair)
            record = _find_record(routes, pair)
            if not all(identity) or not isinstance(record, dict) or identity in existing_identities:
                allowed.append(pair)
                continue

            cooldown_until = _parse_iso(record.get("systemDeleteCooldownUntil"))
            if cooldown_until is not None and resolved_now < cooldown_until:
                suppressed.append({
                    "pair": pair,
                    "reason": "system_cleanup_cooldown",
                    "cooldownUntil": _iso(cooldown_until),
                })
                continue
            if cooldown_until is not None or record.get("systemDeletedAt") or record.get("systemDeleteReason"):
                # Keep the system-deletion provenance until the route is actually
                # recreated (register_auto_created_pair replaces this record) or
                # restored in Astro.  Clearing it merely because the cooldown
                # expired makes the next failed creation attempt look like a
                # manual deletion and incorrectly enables the +20% rearm guard.
                record["missingObservedAt"] = None
                record["missingReferenceOpenPosition"] = None
                record["deletionDetectedAt"] = None
                record["deletionReferenceOpenPosition"] = None
                record["rearmPullbackObservedAt"] = None
                record["rearmPullbackConfirmedAt"] = None
                record["rearmPullbackPctPoints"] = None
                changed = True
                current_value = _open_position(pair)
                try:
                    system_rearm_value = float(pair.get("_systemDeleteRearmOpenPosition"))
                except (TypeError, ValueError):
                    system_rearm_value = float("nan")
                if (
                    math.isfinite(system_rearm_value)
                    and system_rearm_value >= 0
                    and (current_value is None or current_value < system_rearm_value)
                ):
                    suppressed.append({
                        "pair": pair,
                        "reason": "system_cleanup_rearm_threshold",
                        "currentOpenPosition": current_value,
                        "rearmOpenPosition": system_rearm_value,
                    })
                    continue
                allowed.append(pair)
                continue

            reference = record.get("deletionReferenceOpenPosition")
            try:
                reference_value = float(reference) if reference is not None else None
            except (TypeError, ValueError):
                reference_value = None
            if reference_value is not None and math.isfinite(reference_value):
                current_value = _open_position(pair)
                rearm_value = reference_value * (1 + resolved_rearm_pct / 100)
                if record.get("rearmPullbackConfirmedAt"):
                    # The requested pair already passed the ordinary FF/SF rule.
                    # A confirmed configured percentage-point pullback is therefore
                    # sufficient; the separate +20% path is not also required.
                    allowed.append(pair)
                elif current_value is not None and current_value > rearm_value:
                    # Direct-breakout path: no pullback required.
                    allowed.append(pair)
                else:
                    suppressed.append({
                        "pair": pair,
                        "reason": "waiting_for_pullback_or_direct_breakout",
                        "deletionReferenceOpenPosition": reference_value,
                        "rearmOpenPosition": rearm_value,
                        "pullbackRequiredPctPoints": resolved_pullback,
                        "currentOpenPosition": current_value,
                    })
                continue
            if reference is not None:
                record["missingObservedAt"] = None
                record["missingReferenceOpenPosition"] = None
                record["deletionDetectedAt"] = None
                record["deletionReferenceOpenPosition"] = None
                record["rearmPullbackObservedAt"] = None
                record["rearmPullbackConfirmedAt"] = None
                record["rearmPullbackPctPoints"] = None
                changed = True

            if record.get("missingObservedAt"):
                reason = (
                    "missing_spread_reference"
                    if _parse_iso(record.get("missingObservedAt")) is not None
                    and (resolved_now - _parse_iso(record.get("missingObservedAt"))).total_seconds()
                    >= resolved_confirmation
                    and record.get("missingReferenceOpenPosition") is None
                    else "pending_confirmation"
                )
                suppressed.append({"pair": pair, "reason": reason})
                continue
            allowed.append(pair)

        if changed:
            _save_registry(payload)

    return allowed, suppressed, newly_guarded


def check_delete_rearm_before_submit(pair: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Read-only check using the final executable spread, never advancing a guard."""
    now = _utc_now()
    with _registry_lock:
        record = _find_record(_load_registry()["routes"], pair)
        if not isinstance(record, dict):
            return True, {"reason": "first_creation"}
        current = _open_position(pair)
        report = {"currentOpenPosition": current, "previousCardId": record.get("astroPairId"),
                  "previousCreatedAt": record.get("createdAt")}
        cooldown = _parse_iso(record.get("systemDeleteCooldownUntil"))
        if cooldown is not None and now < cooldown:
            return False, {**report, "reason": "system_cleanup_cooldown", "cooldownUntil": _iso(cooldown)}
        if cooldown is not None or record.get("systemDeletedAt") or record.get("systemDeleteReason"):
            try:
                minimum = float(pair.get("_systemDeleteRearmOpenPosition"))
            except (TypeError, ValueError):
                minimum = float('nan')
            allowed = not math.isfinite(minimum) or (current is not None and current >= minimum)
            return allowed, {**report, "reason": "system_cleanup_rearm" if allowed else "system_cleanup_rearm_threshold",
                             "rearmOpenPosition": minimum if math.isfinite(minimum) else None}
        try:
            reference = float(record.get("deletionReferenceOpenPosition"))
        except (TypeError, ValueError):
            reference = float('nan')
        if not math.isfinite(reference) or reference < 0:
            return False, {**report, "reason": "missing_card_not_confirmed"}
        try:
            pullback = float(record.get("rearmPullbackPctPoints"))
        except (TypeError, ValueError):
            pullback = float('nan')
        minimum = reference * (1 + astro_delete_rearm_pct() / 100)
        report.update(deletionReferenceOpenPosition=reference, rearmOpenPosition=minimum)
        if record.get("rearmPullbackConfirmedAt") and math.isfinite(pullback) and pullback >= astro_delete_pullback_pct_points():
            return True, {**report, "reason": "confirmed_pullback", "pullbackPctPoints": pullback}
        allowed = current is not None and current > minimum
        return allowed, {**report, "reason": "direct_breakout" if allowed else "waiting_for_pullback_or_direct_breakout"}


def astro_delete_rearm_status() -> dict[str, Any]:
    active: list[dict[str, Any]] = []
    pending = 0
    with _registry_lock:
        payload = _load_registry()
        for record in payload["routes"].values():
            if not isinstance(record, dict):
                continue
            reference = record.get("deletionReferenceOpenPosition")
            try:
                reference_value = float(reference) if reference is not None else None
            except (TypeError, ValueError):
                reference_value = None
            if reference_value is not None and math.isfinite(reference_value):
                rearm_value = reference_value * (1 + astro_delete_rearm_pct() / 100)
                active.append({
                    "name": record.get("name"),
                    "type": record.get("type"),
                    "buyEx": record.get("buyEx"),
                    "sellEx": record.get("sellEx"),
                    "deletionDetectedAt": record.get("deletionDetectedAt"),
                    "deletionReferenceOpenPosition": reference_value,
                    "rearmOpenPosition": rearm_value,
                    "pullbackRequiredPctPoints": astro_delete_pullback_pct_points(),
                    "observedPullbackPctPoints": record.get("rearmPullbackPctPoints"),
                    "rearmPullbackObservedAt": record.get("rearmPullbackObservedAt"),
                    "rearmPullbackConfirmedAt": record.get("rearmPullbackConfirmedAt"),
                    "state": (
                        "waiting_for_normal_retrigger"
                        if record.get("rearmPullbackConfirmedAt")
                        else "waiting_for_pullback_or_direct_breakout"
                    ),
                })
            elif record.get("missingObservedAt"):
                pending += 1
    active.sort(key=lambda item: str(item.get("deletionDetectedAt") or ""))
    return {
        "rearmPct": astro_delete_rearm_pct(),
        "pullbackPctPoints": astro_delete_pullback_pct_points(),
        "activeGuardCount": len(active),
        "waitingPullbackCount": sum(
            1 for item in active if item["state"] == "waiting_for_pullback_or_direct_breakout"
        ),
        "waitingRetriggerCount": sum(
            1 for item in active if item["state"] == "waiting_for_normal_retrigger"
        ),
        "pendingDeletionCount": pending,
        "items": active,
    }


def astro_cleanup_status() -> dict[str, Any]:
    invalid = 0
    cooldown = 0
    coverage = {"registeredRecords": 0, "legacyProtected": 0, "submittedOnlyProtected": 0, "completeReadbackSnapshots": 0, "inactiveDirectionOnlySnapshots": 0}
    unreadable_fields: set[str] = set()
    now = _utc_now()
    with _registry_lock:
        payload = _load_registry()
        for record in payload["routes"].values():
            if not isinstance(record, dict):
                continue
            coverage["registeredRecords"] += 1
            if record.get("createdPairSnapshotVersion") != AUTO_CARD_SNAPSHOT_VERSION:
                coverage["legacyProtected"] += 1
            elif set(record.get("submittedOnlyConfigFields") or []) - set(record.get("inactiveUnreadableConfigFields") or []):
                coverage["submittedOnlyProtected"] += 1
                unreadable_fields.update(str(field) for field in record["submittedOnlyConfigFields"])
            elif record.get("submittedOnlyConfigFields"):
                coverage["inactiveDirectionOnlySnapshots"] += 1
            else:
                coverage["completeReadbackSnapshots"] += 1
            invalid += int(bool(record.get("invalidObservedAt")))
            cooldown_until = _parse_iso(record.get("systemDeleteCooldownUntil"))
            cooldown += int(cooldown_until is not None and cooldown_until > now)
    return {
        "enabled": True,
        "graceSeconds": astro_cleanup_grace_seconds(),
        "continuousInvalidSeconds": astro_cleanup_invalid_seconds(),
        "systemDeleteCooldownSeconds": astro_cleanup_cooldown_seconds(),
        "systemDeleteRearmBufferPctPoints": astro_cleanup_rearm_buffer_pct_points(),
        "invalidTrackingCount": invalid,
        "cooldownCount": cooldown,
        "configSnapshotCoverage": {**coverage, "unreadableSubmittedFields": sorted(unreadable_fields), "scope": "registered_history_records"},
        "safetyRule": "仅本地自动创建、暂停、从未成交且配置未修改的卡片；旧快照或不可回读的有效配置保守保护。仅当原始和当前priceAlert明确为空时忽略不可回读的仅上涨开关；删除前再次核对状态",
    }


def reset_registry_for_tests() -> None:
    global _memory_registry
    with _registry_lock:
        _memory_registry = _empty_registry()
        path = _registry_path()
        if path and path.exists():
            path.unlink()

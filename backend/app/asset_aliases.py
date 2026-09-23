from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CryptoSymbolMapping

SUPPORTED_CANONICAL_STATUSES = {"verified", "inferred", "manual_review", "conflict"}
FACE_VALUE_PREFIXES = (
    ("10000", 10000.0),
    ("1000", 1000.0),
    ("1M", 1_000_000.0),
    ("1K", 1_000.0),
)


@dataclass(frozen=True)
class AssetCandidate:
    canonical_asset_id: str
    canonical_symbol: str
    canonical_name: str
    status: str
    source: str
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CanonicalAssetResolution:
    input_symbol: str
    normalized_symbol: str
    status: str
    canonical_asset_id: str | None = None
    canonical_symbol: str | None = None
    canonical_name: str | None = None
    candidates: list[AssetCandidate] = field(default_factory=list)
    message: str | None = None

    @property
    def usable_for_scan(self) -> bool:
        return self.status in {"verified", "inferred"} and self.canonical_symbol is not None


@dataclass(frozen=True)
class ExchangeSymbolResolution:
    input_symbol: str
    normalized_symbol: str
    exchange: str
    market_type: str
    request_symbol: str
    display_symbol: str
    price_ratio: float
    status: str
    canonical_asset_id: str | None = None
    canonical_symbol: str | None = None
    source: str = "identity"
    evidence: str | None = None
    message: str | None = None

    @property
    def usable_for_scan(self) -> bool:
        return self.status in {"verified", "inferred", "identity"}


def default_asset_alias_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "asset_aliases.json"


def normalize_asset_symbol(value: str) -> str:
    symbol = (value or "").strip().upper()
    symbol = re.sub(r"[\s_\-/]", "", symbol)
    if symbol.endswith("USDT") and len(symbol) > 4:
        symbol = symbol[:-4]
    if not symbol or not symbol.isalnum():
        raise ValueError("币种格式不正确")
    return symbol


def normalize_exchange_code(value: str) -> str:
    return (value or "").strip().lower()


def normalize_market_type(value: str | None) -> str:
    market_type = (value or "futures").strip().lower()
    if market_type not in {"spot", "futures"}:
        raise ValueError("市场类型仅支持 spot、futures")
    return market_type


def load_asset_alias_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or default_asset_alias_path()
    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("资产映射配置必须是 JSON object")
    return config


def _asset_candidates(config: dict[str, Any], normalized_symbol: str) -> list[AssetCandidate]:
    candidates: list[AssetCandidate] = []
    for raw_asset in config.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        aliases = {normalize_asset_symbol(str(item)) for item in raw_asset.get("aliases") or []}
        canonical_symbol = normalize_asset_symbol(str(raw_asset.get("canonicalSymbol") or ""))
        aliases.add(canonical_symbol)
        if normalized_symbol not in aliases:
            continue
        status = str(raw_asset.get("status") or "manual_review")
        if status not in SUPPORTED_CANONICAL_STATUSES:
            status = "manual_review"
        candidates.append(
            AssetCandidate(
                canonical_asset_id=str(raw_asset.get("canonicalAssetId") or ""),
                canonical_symbol=canonical_symbol,
                canonical_name=str(raw_asset.get("canonicalName") or canonical_symbol),
                status=status,
                source="config",
                evidence=[item for item in raw_asset.get("evidence") or [] if isinstance(item, dict)],
            )
        )
    return candidates


def _face_value_candidate(config: dict[str, Any], normalized_symbol: str) -> tuple[str, float] | None:
    for prefix, ratio in FACE_VALUE_PREFIXES:
        if not normalized_symbol.startswith(prefix) or len(normalized_symbol) <= len(prefix):
            continue
        base_symbol = normalized_symbol[len(prefix) :]
        if _asset_candidates(config, base_symbol):
            return base_symbol, ratio
    return None


def resolve_canonical_asset(symbol: str, config: dict[str, Any] | None = None) -> CanonicalAssetResolution:
    loaded = config or load_asset_alias_config()
    normalized = normalize_asset_symbol(symbol)
    candidates = _asset_candidates(loaded, normalized)
    if len(candidates) == 1:
        candidate = candidates[0]
        if candidate.status in {"verified", "inferred"}:
            return CanonicalAssetResolution(
                input_symbol=symbol,
                normalized_symbol=normalized,
                status=candidate.status,
                canonical_asset_id=candidate.canonical_asset_id,
                canonical_symbol=candidate.canonical_symbol,
                canonical_name=candidate.canonical_name,
                candidates=candidates,
            )
        return CanonicalAssetResolution(
            input_symbol=symbol,
            normalized_symbol=normalized,
            status=candidate.status,
            candidates=candidates,
            message="该币种需要人工确认，禁止自动扫描和推送。",
        )
    if len(candidates) > 1:
        return CanonicalAssetResolution(
            input_symbol=symbol,
            normalized_symbol=normalized,
            status="manual_review",
            candidates=candidates,
            message="同一 symbol 命中多个资产，不能自动合并。",
        )
    face_value = _face_value_candidate(loaded, normalized)
    if face_value:
        base_symbol, _ratio = face_value
        base = resolve_canonical_asset(base_symbol, loaded)
        return CanonicalAssetResolution(
            input_symbol=symbol,
            normalized_symbol=normalized,
            status=base.status if base.usable_for_scan else "manual_review",
            canonical_asset_id=base.canonical_asset_id,
            canonical_symbol=base.canonical_symbol,
            canonical_name=base.canonical_name,
            candidates=base.candidates,
            message="面值前缀只作为交易所执行映射，不创建新资产。",
        )
    return CanonicalAssetResolution(
        input_symbol=symbol,
        normalized_symbol=normalized,
        status="manual_review",
        candidates=[],
        message="未找到可靠资产身份，需人工确认后再参与扫描。",
    )


def _iter_exchange_mappings(asset: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for mapping in asset.get("exchangeMappings") or []:
        if isinstance(mapping, dict):
            yield mapping


def _find_config_exchange_mapping(
    config: dict[str, Any],
    canonical_asset_id: str | None,
    exchange: str,
    market_type: str,
) -> dict[str, Any] | None:
    if not canonical_asset_id:
        return None
    for asset in config.get("assets") or []:
        if not isinstance(asset, dict) or asset.get("canonicalAssetId") != canonical_asset_id:
            continue
        for mapping in _iter_exchange_mappings(asset):
            if normalize_exchange_code(str(mapping.get("exchange") or "")) != exchange:
                continue
            if normalize_market_type(str(mapping.get("marketType") or "futures")) != market_type:
                continue
            return mapping
    return None


def _db_exchange_mapping(
    db: Session | None,
    symbol: str,
    exchange: str,
    market_type: str,
) -> CryptoSymbolMapping | None:
    if db is None:
        return None
    return db.scalar(
        select(CryptoSymbolMapping)
        .where(
            CryptoSymbolMapping.input_symbol == symbol,
            CryptoSymbolMapping.exchange == exchange,
            CryptoSymbolMapping.market_type == market_type,
        )
        .limit(1)
    )


def resolve_exchange_symbol(
    symbol: str,
    exchange: str,
    market_type: str,
    db: Session | None = None,
    config: dict[str, Any] | None = None,
) -> ExchangeSymbolResolution:
    loaded = config or load_asset_alias_config()
    normalized = normalize_asset_symbol(symbol)
    normalized_exchange = normalize_exchange_code(exchange)
    normalized_market_type = normalize_market_type(market_type)
    canonical = resolve_canonical_asset(normalized, loaded)

    db_mapping = _db_exchange_mapping(db, normalized, normalized_exchange, normalized_market_type)
    if db_mapping is not None:
        return ExchangeSymbolResolution(
            input_symbol=symbol,
            normalized_symbol=normalized,
            exchange=normalized_exchange,
            market_type=normalized_market_type,
            request_symbol=normalize_asset_symbol(db_mapping.mapped_symbol),
            display_symbol=canonical.canonical_symbol or normalized,
            price_ratio=float(db_mapping.price_ratio or 1.0),
            status="verified" if canonical.usable_for_scan else canonical.status,
            canonical_asset_id=canonical.canonical_asset_id,
            canonical_symbol=canonical.canonical_symbol,
            source="db",
            evidence=db_mapping.note or "crypto_symbol_mappings",
            message=canonical.message,
        )

    config_mapping = _find_config_exchange_mapping(
        loaded,
        canonical.canonical_asset_id,
        normalized_exchange,
        normalized_market_type,
    )
    if config_mapping is not None:
        status = str(config_mapping.get("status") or canonical.status)
        if status not in SUPPORTED_CANONICAL_STATUSES:
            status = "manual_review"
        return ExchangeSymbolResolution(
            input_symbol=symbol,
            normalized_symbol=normalized,
            exchange=normalized_exchange,
            market_type=normalized_market_type,
            request_symbol=normalize_asset_symbol(str(config_mapping.get("symbol") or normalized)),
            display_symbol=canonical.canonical_symbol or normalized,
            price_ratio=float(config_mapping.get("priceRatio") or 1.0),
            status=status,
            canonical_asset_id=canonical.canonical_asset_id,
            canonical_symbol=canonical.canonical_symbol,
            source="config",
            evidence=str(config_mapping.get("evidence") or ""),
            message=canonical.message,
        )

    if canonical.usable_for_scan:
        return ExchangeSymbolResolution(
            input_symbol=symbol,
            normalized_symbol=normalized,
            exchange=normalized_exchange,
            market_type=normalized_market_type,
            request_symbol=canonical.canonical_symbol or normalized,
            display_symbol=canonical.canonical_symbol or normalized,
            price_ratio=1.0,
            status="identity",
            canonical_asset_id=canonical.canonical_asset_id,
            canonical_symbol=canonical.canonical_symbol,
            source="identity",
            message=canonical.message,
        )

    return ExchangeSymbolResolution(
        input_symbol=symbol,
        normalized_symbol=normalized,
        exchange=normalized_exchange,
        market_type=normalized_market_type,
        request_symbol=normalized,
        display_symbol=normalized,
        price_ratio=1.0,
        status=canonical.status,
        canonical_asset_id=canonical.canonical_asset_id,
        canonical_symbol=canonical.canonical_symbol,
        source="unresolved",
        message=canonical.message,
    )


def apply_price_ratio(value: float | None, price_ratio: float | None) -> float | None:
    if value is None:
        return None
    ratio = price_ratio or 1.0
    if ratio <= 0:
        raise ValueError("价格汇率必须大于 0")
    return value / ratio


def validate_asset_alias_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen_assets: set[str] = set()
    mapping_owner: dict[tuple[str, str, str], str] = {}
    alias_owner: dict[str, set[str]] = {}

    for raw_asset in config.get("assets") or []:
        if not isinstance(raw_asset, dict):
            issues.append({"level": "error", "code": "asset_shape", "message": "资产项必须是 object"})
            continue
        asset_id = str(raw_asset.get("canonicalAssetId") or "")
        if not asset_id:
            issues.append({"level": "error", "code": "missing_asset_id", "message": "缺少 canonicalAssetId"})
            continue
        if asset_id in seen_assets:
            issues.append({"level": "error", "code": "duplicate_asset_id", "assetId": asset_id})
        seen_assets.add(asset_id)
        status = str(raw_asset.get("status") or "")
        if status not in SUPPORTED_CANONICAL_STATUSES:
            issues.append({"level": "error", "code": "invalid_status", "assetId": asset_id, "status": status})
        aliases = set(str(item) for item in raw_asset.get("aliases") or [])
        aliases.add(str(raw_asset.get("canonicalSymbol") or ""))
        for alias in aliases:
            try:
                normalized_alias = normalize_asset_symbol(alias)
            except ValueError:
                issues.append({"level": "error", "code": "invalid_alias", "assetId": asset_id, "alias": alias})
                continue
            alias_owner.setdefault(normalized_alias, set()).add(asset_id)
        for mapping in _iter_exchange_mappings(raw_asset):
            try:
                key = (
                    normalize_exchange_code(str(mapping.get("exchange") or "")),
                    normalize_market_type(str(mapping.get("marketType") or "")),
                    normalize_asset_symbol(str(mapping.get("symbol") or "")),
                )
            except ValueError as exc:
                issues.append({"level": "error", "code": "invalid_exchange_mapping", "assetId": asset_id, "message": str(exc)})
                continue
            previous = mapping_owner.get(key)
            if previous and previous != asset_id:
                issues.append(
                    {
                        "level": "error",
                        "code": "exchange_mapping_conflict",
                        "exchange": key[0],
                        "marketType": key[1],
                        "symbol": key[2],
                        "assetIds": sorted([previous, asset_id]),
                    }
                )
            mapping_owner[key] = asset_id
            price_ratio = mapping.get("priceRatio", 1)
            try:
                if float(price_ratio) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append({"level": "error", "code": "invalid_price_ratio", "assetId": asset_id, "symbol": key[2]})

    for alias, owners in alias_owner.items():
        if len(owners) > 1:
            issues.append(
                {
                    "level": "warning",
                    "code": "same_alias_multiple_assets",
                    "alias": alias,
                    "assetIds": sorted(owners),
                    "message": "同一 symbol 命中多个资产，解析时会进入 manual_review。",
                }
            )

    return issues


def db_symbol_mappings_as_config_issues(db: Session) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(CryptoSymbolMapping)))
    by_scope: dict[tuple[str, str, str], set[str]] = {}
    issues: list[dict[str, Any]] = []
    for row in rows:
        key = (row.exchange, row.market_type, row.mapped_symbol)
        by_scope.setdefault(key, set()).add(row.input_symbol)
        if not row.price_ratio or row.price_ratio <= 0:
            issues.append(
                {
                    "level": "error",
                    "code": "db_invalid_price_ratio",
                    "mappingId": row.id,
                    "symbol": row.input_symbol,
                }
            )
    for (exchange, market_type, mapped_symbol), input_symbols in by_scope.items():
        if len(input_symbols) > 1:
            issues.append(
                {
                    "level": "error",
                    "code": "db_exchange_mapping_conflict",
                    "exchange": exchange,
                    "marketType": market_type,
                    "mappedSymbol": mapped_symbol,
                    "inputSymbols": sorted(input_symbols),
                }
            )
    return issues


def verification_summary(config: dict[str, Any], db: Session | None = None) -> dict[str, Any]:
    issues = validate_asset_alias_config(config)
    if db is not None:
        issues.extend(db_symbol_mappings_as_config_issues(db))
    assets = [asset for asset in config.get("assets") or [] if isinstance(asset, dict)]
    statuses: dict[str, int] = {}
    for asset in assets:
        status = str(asset.get("status") or "manual_review")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "assetCount": len(assets),
        "statuses": statuses,
        "issueCount": len(issues),
        "errorCount": sum(1 for issue in issues if issue.get("level") == "error"),
        "warningCount": sum(1 for issue in issues if issue.get("level") == "warning"),
        "issues": issues,
    }

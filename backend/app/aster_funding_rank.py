from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.asset_aliases import load_asset_alias_config
from app.database import SessionLocal, get_db
from app.models import AsterFundingRankSnapshot

router = APIRouter(prefix="/api/astro/aster-funding-rank", tags=["aster-funding-rank"])
logger = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
RANK_INTERVAL_HOURS = 8
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000
PUBLISH_DELAY = timedelta(minutes=5)
RETRY_DELAY = timedelta(minutes=5)
CHAIN_IDS = {"ethereum": "1", "eth": "1", "bsc": "56", "solana": "501",
             "base": "8453", "arbitrum": "42161", "robinhood": "4663"}
EVM_CHAINS = {"1", "56", "8453", "42161", "4663"}
_stop = threading.Event()
_thread: threading.Thread | None = None
_attempted_ms = 0
_attempted_at = datetime.min.replace(tzinfo=timezone.utc)
_state: dict[str, Any] = {"running": False, "error": None, "eligibleCount": None}


def _utc_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def latest_cutoff(now: datetime) -> datetime:
    local = now.astimezone(BEIJING)
    return local.replace(hour=(local.hour // RANK_INTERVAL_HOURS) * RANK_INTERVAL_HOURS,
                         minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _valid_address(chain_id: str, value: Any) -> str | None:
    address = str(value or "").strip()
    if chain_id in EVM_CHAINS and re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        return address
    if chain_id == "501" and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
        return address
    return None


def mapped_addresses() -> dict[str, list[dict[str, str]]]:
    from app.astro_spread_scanner import spread_scan_dex_mapped_assets

    candidates: dict[str, dict[tuple[str, str], dict[str, str]]] = {}

    def add(symbol: Any, chain_id: str, address: Any, source: str) -> None:
        token = str(symbol or "").strip().upper()
        valid = _valid_address(chain_id, address)
        if not token or not valid:
            return
        identity = valid if chain_id == "501" else valid.lower()
        candidates.setdefault(token, {})[(chain_id, identity)] = {
            "chainId": chain_id, "address": valid, "source": source,
        }

    for item in spread_scan_dex_mapped_assets():
        add(item.get("symbol"), str(item.get("chainIndex") or ""), item.get("contractAddress"), "Astro 已配置")

    for asset in load_asset_alias_config().get("assets") or []:
        if not isinstance(asset, dict) or asset.get("status") != "verified":
            continue
        for network in asset.get("networks") or []:
            if isinstance(network, dict) and network.get("status") == "verified":
                add(asset.get("canonicalSymbol"), CHAIN_IDS.get(str(network.get("network") or "").lower(), ""),
                    network.get("contractAddress"), "资产映射已核验")

    result: dict[str, list[dict[str, str]]] = {}
    for token, rows in candidates.items():
        by_chain: dict[str, list[dict[str, str]]] = {}
        for item in rows.values():
            by_chain.setdefault(item["chainId"], []).append(item)
        safe = [items[0] for items in by_chain.values() if len(items) == 1]
        if safe:
            result[token] = sorted(safe, key=lambda item: (item["chainId"], item["address"]))
    return result


def _rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Aster 结算费率无效") from exc
    if not rate.is_finite() or abs(rate) > 1:
        raise ValueError("Aster 结算费率越界")
    return rate


def _settlement_ms(value: Any) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Aster 结算时间无效") from exc
    if raw <= 0:
        raise ValueError("Aster 结算时间无效")
    # Aster occasionally timestamps a scheduled settlement a millisecond late.
    return raw // 60000 * 60000 if raw % 60000 < 5000 else raw


def rank_symbol(
    market: dict[str, Any], history: list[dict[str, Any]], interval_hours: int,
    cutoff_ms: int, addresses: list[dict[str, str]],
) -> dict[str, Any] | None:
    symbol = market["symbol"]
    start_ms = cutoff_ms - DAY_MS
    if len(history) >= 1000:
        raise ValueError(f"{symbol} 历史费率达到接口上限")
    entries: dict[int, Decimal] = {}
    for item in history:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            raise ValueError(f"{symbol} 历史费率币种不匹配")
        settled = _settlement_ms(item.get("fundingTime"))
        if start_ms < settled <= cutoff_ms:
            rate = _rate(item.get("fundingRate"))
            if settled in entries and entries[settled] != rate:
                raise ValueError(f"{symbol} 同一结算时间费率冲突")
            entries[settled] = rate
    times = sorted(entries)
    onboard_ms = int(market.get("onboardDate") or 0)
    if not times:
        if onboard_ms > start_ms:
            return None
        raise ValueError(f"{symbol} 缺少过去24小时结算记录")
    max_gap_ms = interval_hours * 60 * 60 * 1000 + 5 * 60 * 1000
    if cutoff_ms - times[-1] > max_gap_ms:
        raise ValueError(f"{symbol} 最近结算记录过期")
    if any(right - left > max_gap_ms for left, right in zip(times, times[1:])):
        raise ValueError(f"{symbol} 结算记录有缺口")
    if onboard_ms <= start_ms and times[0] - start_ms > max_gap_ms:
        raise ValueError(f"{symbol} 窗口开始处缺少结算记录")
    total = sum(entries.values(), Decimal("0"))
    if total <= 0:
        return None
    return {
        "symbol": symbol,
        "coin": market["baseAsset"],
        "totalRatePct": float(total * 100),
        "settlementCount": len(times),
        "latestSettlementAt": _iso_ms(times[-1]),
        "listedWithin24h": onboard_ms > start_ms,
        "addresses": addresses,
        "settlements": [{"at": _iso_ms(ms), "ratePct": float(entries[ms] * 100)} for ms in times],
    }


def fetch_market_data() -> tuple[list[dict[str, Any]], dict[str, int]]:
    from app.crypto import base_url, http_client, request_json

    with http_client(timeout=12) as client:
        markets = request_json(client, f"{base_url('as')}/fapi/v3/exchangeInfo")
        funding = request_json(client, f"{base_url('as')}/fapi/v3/fundingInfo")
    if not isinstance(markets, dict) or not isinstance(markets.get("symbols"), list) or not isinstance(funding, list):
        raise ValueError("Aster 市场或资金费周期接口返回异常")
    active = [item for item in markets["symbols"] if isinstance(item, dict)
              and item.get("status") == "TRADING" and item.get("contractType") == "PERPETUAL"
              and item.get("quoteAsset") == "USDT" and isinstance(item.get("symbol"), str)
              and isinstance(item.get("baseAsset"), str)]
    intervals = {str(item.get("symbol")): int(item["fundingIntervalHours"])
                 for item in funding if isinstance(item, dict) and item.get("symbol")
                 and str(item.get("fundingIntervalHours") or "").isdigit()}
    if not active:
        raise ValueError("Aster 未返回在交易的 USDT 永续合约")
    return active, intervals


def fetch_hour_history(start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    from app.crypto import base_url, http_client, request_json

    with http_client(timeout=10) as client:
        rows = request_json(client, f"{base_url('as')}/fapi/v3/fundingRate", {
            "startTime": start_ms + 5000, "endTime": end_ms + 4999, "limit": 1000,
        })
    if not isinstance(rows, list) or len(rows) >= 1000:
        raise ValueError(f"Aster 全市场历史费率窗口返回异常或被截断：{_iso_ms(end_ms)}")
    return rows


def fetch_all_history(cutoff_ms: int) -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="aster-funding-rank") as pool:
        futures = [pool.submit(fetch_hour_history, cutoff_ms - DAY_MS + hour * HOUR_MS,
                               cutoff_ms - DAY_MS + (hour + 1) * HOUR_MS) for hour in range(24)]
        for future in as_completed(futures):
            for item in future.result():
                if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
                    raise ValueError("Aster 全市场历史费率数据格式异常")
                history.setdefault(item["symbol"], []).append(item)
    return history


def quote_volume_from_klines(symbol: str, candles: Any, cutoff_ms: int, onboard_ms: int) -> float:
    if not isinstance(candles, list):
        raise ValueError(f"{symbol} 成交额K线接口返回异常")
    start_ms = cutoff_ms - DAY_MS
    expected_first = max(start_ms, onboard_ms // HOUR_MS * HOUR_MS)
    volumes: dict[int, Decimal] = {}
    for candle in candles:
        if not isinstance(candle, list) or len(candle) < 8:
            raise ValueError(f"{symbol} 成交额K线格式异常")
        try:
            opened = int(candle[0])
            closed = int(candle[6])
            volume = Decimal(str(candle[7]))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"{symbol} 成交额K线数值无效") from exc
        if (opened < start_ms or opened >= cutoff_ms or opened % HOUR_MS != 0
                or closed != opened + HOUR_MS - 1 or not volume.is_finite() or volume < 0):
            raise ValueError(f"{symbol} 成交额K线超出窗口或数值无效")
        if opened in volumes:
            raise ValueError(f"{symbol} 成交额K线重复")
        volumes[opened] = volume
    expected = set(range(expected_first, cutoff_ms, HOUR_MS))
    if set(volumes) != expected:
        raise ValueError(f"{symbol} 过去24小时成交额K线不完整")
    return float(sum(volumes.values(), Decimal("0")))


def fetch_quote_volume(symbol: str, cutoff_ms: int, onboard_ms: int) -> float:
    from app.crypto import base_url, http_client, request_json

    with http_client(timeout=10) as client:
        candles = request_json(client, f"{base_url('as')}/fapi/v3/klines", {
            "symbol": symbol, "interval": "1h", "startTime": cutoff_ms - DAY_MS,
            "endTime": cutoff_ms - 1, "limit": 24,
        })
    return quote_volume_from_klines(symbol, candles, cutoff_ms, onboard_ms)


def generate_snapshot(cutoff: datetime) -> dict[str, Any]:
    cutoff_ms = _utc_ms(cutoff)
    addresses_by_coin = mapped_addresses()
    markets, intervals = fetch_market_data()
    eligible = [item for item in markets if item["baseAsset"].upper() in addresses_by_coin]
    _state["eligibleCount"] = len(eligible)
    if not eligible:
        raise ValueError("Aster 当前没有匹配已配置链上地址的合约")
    if any(not 1 <= intervals.get(item["symbol"], 0) <= 24 for item in eligible):
        raise ValueError("部分 Aster 合约缺少有效的资金费结算周期")

    history_by_symbol = fetch_all_history(cutoff_ms)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in eligible:
        try:
            ranked = rank_symbol(item, history_by_symbol.get(item["symbol"], []), intervals[item["symbol"]], cutoff_ms,
                                 addresses_by_coin[item["baseAsset"].upper()])
            if ranked:
                rows.append(ranked)
        except Exception as exc:
            errors.append(f"{item['symbol']}: {type(exc).__name__}: {exc}")
    if errors:
        raise ValueError(f"{len(errors)}/{len(eligible)} 个合约无法核验；" + "; ".join(errors[:3]))
    rows.sort(key=lambda item: (-Decimal(str(item["totalRatePct"])), item["symbol"]))
    market_by_symbol = {item["symbol"]: item for item in eligible}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="aster-funding-volume") as pool:
        futures = {pool.submit(fetch_quote_volume, item["symbol"], cutoff_ms,
                               int(market_by_symbol[item["symbol"]].get("onboardDate") or 0)): item
                   for item in rows[:50]}
        for future in as_completed(futures):
            item = futures[future]
            item["volume24hUsdt"] = future.result()
    snapshot = {
        "asOf": _iso_ms(cutoff_ms), "generatedAt": _iso_ms(_utc_ms(datetime.now(timezone.utc))),
        "marketCount": len(markets), "eligibleCount": len(eligible),
        "rankedCount": len(rows), "rows": rows,
    }
    with SessionLocal() as db:
        existing = db.scalar(select(AsterFundingRankSnapshot).where(AsterFundingRankSnapshot.as_of_ms == cutoff_ms))
        if existing is None:
            db.add(AsterFundingRankSnapshot(
                as_of_ms=cutoff_ms, generated_at_ms=_utc_ms(datetime.now(timezone.utc)),
                market_count=len(markets), eligible_count=len(eligible), ranked_count=len(rows),
                rows_json=json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            ))
            db.execute(delete(AsterFundingRankSnapshot).where(AsterFundingRankSnapshot.as_of_ms < cutoff_ms - 90 * DAY_MS))
            db.commit()
    return snapshot


def _snapshot_for(db: Session, as_of_ms: int | None = None) -> AsterFundingRankSnapshot | None:
    query = select(AsterFundingRankSnapshot)
    if as_of_ms is not None:
        query = query.where(AsterFundingRankSnapshot.as_of_ms == as_of_ms)
    return db.scalar(query.order_by(desc(AsterFundingRankSnapshot.as_of_ms)).limit(1))


@router.get("")
def get_aster_funding_rank(limit: int = 20, db: Session = Depends(get_db)) -> dict[str, Any]:
    count = limit if limit in {10, 20, 50} else 20
    snapshot = _snapshot_for(db)
    now = datetime.now(timezone.utc)
    cutoff = latest_cutoff(now)
    payload: dict[str, Any] = {
        "status": "ready" if snapshot else "pending", "scope": "aster_mapped_usdt_perpetual",
        "limit": count, "nextCutoff": _iso_ms(_utc_ms(cutoff + timedelta(hours=RANK_INTERVAL_HOURS))),
        "currentCutoff": _iso_ms(_utc_ms(cutoff)), "running": _state["running"],
        "lastError": _state["error"],
        "asOf": None, "generatedAt": None, "marketCount": None,
        "eligibleCount": _state["eligibleCount"], "rankedCount": 0, "rows": [],
    }
    if snapshot:
        payload.update(asOf=_iso_ms(snapshot.as_of_ms), generatedAt=_iso_ms(snapshot.generated_at_ms),
                       marketCount=snapshot.market_count, eligibleCount=snapshot.eligible_count,
                       rankedCount=snapshot.ranked_count, rows=json.loads(snapshot.rows_json)[:count])
        if snapshot.as_of_ms < _utc_ms(cutoff) and now >= cutoff + PUBLISH_DELAY:
            payload["status"] = "delayed"
    return payload


def _scheduler_loop() -> None:
    global _attempted_ms, _attempted_at
    while not _stop.is_set():
        now = datetime.now(timezone.utc)
        cutoff = latest_cutoff(now)
        cutoff_ms = _utc_ms(cutoff)
        if now >= cutoff + PUBLISH_DELAY and (_attempted_ms != cutoff_ms or now - _attempted_at >= RETRY_DELAY):
            with SessionLocal() as db:
                existing = _snapshot_for(db, cutoff_ms)
            if existing is None:
                _attempted_ms, _attempted_at = cutoff_ms, now
                _state.update(running=True, error=None)
                try:
                    generate_snapshot(cutoff)
                except Exception as exc:
                    _state["error"] = str(exc)
                    logger.warning("Aster funding ranking failed for %s: %s", cutoff.isoformat(), exc)
                finally:
                    _state["running"] = False
        _stop.wait(30)


def start_scheduler() -> None:
    global _thread
    if os.environ.get("ASTER_FUNDING_RANK_ENABLED", "1").strip().lower() in {"0", "false", "off"}:
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_scheduler_loop, name="aster-funding-rank", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)

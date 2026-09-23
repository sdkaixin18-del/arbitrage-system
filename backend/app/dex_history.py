from __future__ import annotations

import math
import re
import time
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query


router = APIRouter(prefix="/api/dex-history", tags=["dex-history"])

SUPPORTED_RANGES = {4, 24, 72, 168, 720}
SOLANA_ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9\u4e00-\u9fff.]{1,32}$")
DEX_NETWORKS: dict[str, str] = {
    "solana": "Solana",
    "bsc": "BNB Smart Chain",
    "eth": "Ethereum",
    "base": "Base",
    "arbitrum": "Arbitrum",
    "robinhood": "Robinhood Chain",
}
DEX_NETWORK_ALIASES = {
    "solana": "solana",
    "bsc": "bsc",
    "bnb smart chain": "bsc",
    "bnb-smart-chain": "bsc",
    "eth": "eth",
    "ethereum": "eth",
    "base": "base",
    "arbitrum": "arbitrum",
    "robinhood": "robinhood",
    "robinhood chain": "robinhood",
}
FUTURES_VENUES: dict[str, str] = {
    "bg": "Bitget",
    "bn": "Binance",
    "gt": "Gate",
    "okx": "OKX",
    "aster": "Aster",
    "by": "Bybit",
}
FUTURES_VENUE_ALIASES = {
    "bg": "bg",
    "bitget": "bg",
    "bn": "bn",
    "binance": "bn",
    "gt": "gt",
    "gate": "gt",
    "gateio": "gt",
    "okx": "okx",
    "aster": "aster",
    "as": "aster",
    "by": "by",
    "bybit": "by",
}


def normalize_dex_network(value: str) -> str:
    network = DEX_NETWORK_ALIASES.get(value.strip().lower())
    if network is None:
        raise ValueError("DEX 链只支持 Solana、BNB Smart Chain、Ethereum、Base、Arbitrum 或 Robinhood Chain")
    return network


def normalize_pool_address(value: str, network: str = "solana") -> str:
    normalized_network = normalize_dex_network(network)
    raw = value.strip()
    candidate = raw.rstrip("/")
    if "://" in raw:
        parsed = urlparse(raw)
        parts = [part for part in parsed.path.split("/") if part]
        if "pools" not in parts:
            raise ValueError("GeckoTerminal 链接中缺少池地址")
        pool_index = parts.index("pools")
        if pool_index == 0 or pool_index + 1 >= len(parts):
            raise ValueError("GeckoTerminal 链接格式不正确")
        link_network = normalize_dex_network(parts[pool_index - 1])
        if link_network != normalized_network:
            raise ValueError(f"链接属于 {DEX_NETWORKS[link_network]}，与所选 {DEX_NETWORKS[normalized_network]} 不一致")
        candidate = parts[pool_index + 1]
    else:
        candidate = candidate.split("?", 1)[0].split("#", 1)[0]
    # EVM addresses are hexadecimal and must be matched case-insensitively.
    # GeckoTerminal canonicalizes resource ids to lowercase, so keeping a
    # pasted checksum/uppercase form here would make a valid token appear to
    # have no pools. Solana base58 addresses remain case-sensitive.
    if normalized_network != "solana":
        candidate = candidate.lower()
    pattern = SOLANA_ADDRESS_PATTERN if normalized_network == "solana" else EVM_ADDRESS_PATTERN
    if not pattern.fullmatch(candidate):
        address_kind = "Solana" if normalized_network == "solana" else "EVM 0x"
        raise ValueError(f"请输入有效的 {address_kind} 池地址或 GeckoTerminal 池链接")
    return candidate


def normalize_futures_venue(value: str) -> str:
    venue = FUTURES_VENUE_ALIASES.get(value.strip().lower())
    if venue is None:
        raise ValueError("右侧交易所只支持 BG、BN、GT、OKX、Aster 或 BY")
    return venue


def normalize_futures_symbol(value: str, venue: str) -> tuple[str, str]:
    normalized_venue = normalize_futures_venue(venue)
    raw = re.sub(r"[/_\-\s]", "", value.strip().upper())
    if raw.endswith("USDTSWAP"):
        base_symbol = raw[:-8]
    elif raw.endswith("USDT"):
        base_symbol = raw[:-4]
    else:
        base_symbol = raw
    if not SYMBOL_PATTERN.fullmatch(base_symbol):
        raise ValueError("币名只支持 1 至 32 位中文、英文字母、数字或点号")
    if normalized_venue == "gt":
        return base_symbol, f"{base_symbol}_USDT"
    if normalized_venue == "okx":
        return base_symbol, f"{base_symbol}-USDT-SWAP"
    return base_symbol, f"{base_symbol}USDT"


def _granularity(range_hours: int) -> tuple[str, int]:
    if range_hours <= 24:
        return "1m", 60_000
    if range_hours <= 168:
        return "5m", 300_000
    return "15m", 900_000


def astro_symmetric_spread_pct(buy_price: float, sell_price: float) -> float | None:
    """Match Astro's symmetric spread direction: buy leg first, sell leg second."""
    denominator = buy_price + sell_price
    if buy_price <= 0 or sell_price <= 0 or denominator <= 0:
        return None
    value = 2 * (sell_price - buy_price) / denominator * 100
    return value if -100 < value < 100 else None


def _json_get(client: httpx.Client, url: str, params: dict[str, Any]) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(url, params=params)
            if response.is_success:
                return response.json()
            if response.status_code != 429 and response.status_code < 500:
                upstream_message = ""
                try:
                    error_payload = response.json()
                    if isinstance(error_payload, dict):
                        upstream_message = str(
                            error_payload.get("msg")
                            or error_payload.get("message")
                            or error_payload.get("error")
                            or ""
                        ).strip()
                except (TypeError, ValueError):
                    pass
                suffix = f"：{upstream_message}" if upstream_message else ""
                raise RuntimeError(f"云端历史行情返回 {response.status_code}{suffix}")
            last_error = RuntimeError(f"云端历史行情返回 {response.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(str(last_error or "云端历史行情读取失败"))


def _fetch_dex_rows(
    client: httpx.Client,
    pair: dict[str, Any],
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    aggregate = interval_ms // 60_000
    rows: dict[int, float] = {}
    before_timestamp = end_ms // 1000
    for _page in range(5):
        payload = _json_get(
            client,
            f"https://api.geckoterminal.com/api/v2/networks/{pair['dexNetwork']}/pools/{pair['poolAddress']}/ohlcv/minute",
            {
                "aggregate": aggregate,
                "limit": 1000,
                "currency": "usd",
                "token": pair["dexTokenSide"],
                "before_timestamp": before_timestamp,
            },
        )
        page_rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not page_rows:
            break
        oldest_ms: int | None = None
        for item in page_rows:
            if not isinstance(item, list) or len(item) < 5:
                continue
            timestamp_ms = int(float(item[0]) * 1000)
            close = float(item[4])
            if not math.isfinite(close) or close <= 0:
                continue
            oldest_ms = timestamp_ms if oldest_ms is None else min(oldest_ms, timestamp_ms)
            if start_ms <= timestamp_ms <= end_ms:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
        if oldest_ms is None or oldest_ms <= start_ms:
            break
        before_timestamp = oldest_ms // 1000 - 1
    if len(rows) < 2:
        raise RuntimeError(f"{pair['symbol']} {pair['dexNetworkLabel']} 主池历史 K 线不足")
    return rows


def _fetch_dex_pool_metadata(
    client: httpx.Client,
    pool_address: str,
    requested_symbol: str,
    network: str = "solana",
) -> dict[str, str]:
    payload = _json_get(
        client,
        f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}",
        {"include": "base_token,quote_token,dex"},
    )
    data = (payload or {}).get("data") or {}
    relationships = data.get("relationships") or {}
    base_id = (((relationships.get("base_token") or {}).get("data") or {}).get("id"))
    quote_id = (((relationships.get("quote_token") or {}).get("data") or {}).get("id"))
    included = {
        item.get("id"): (item.get("attributes") or {})
        for item in (payload or {}).get("included") or []
        if isinstance(item, dict)
    }
    base_symbol = str((included.get(base_id) or {}).get("symbol") or "").upper()
    quote_symbol = str((included.get(quote_id) or {}).get("symbol") or "").upper()
    if requested_symbol == base_symbol:
        token_side = "base"
    elif requested_symbol == quote_symbol:
        token_side = "quote"
    else:
        pool_pair = " / ".join(value for value in (base_symbol, quote_symbol) if value) or "未知币种"
        raise RuntimeError(f"池内未找到 {requested_symbol}；该池识别为 {pool_pair}")
    pool_name = str((data.get("attributes") or {}).get("name") or f"{base_symbol} / {quote_symbol}")
    return {
        "tokenSide": token_side,
        "poolDex": str((((relationships.get("dex") or {}).get("data") or {}).get("id")) or "未知池来源"),
        "poolName": pool_name,
        "baseSymbol": base_symbol,
        "quoteSymbol": quote_symbol,
        # Launchpads may expose a pool under the token address itself. This
        # remains a token query: follow its liquid trading pool after migration.
        "inputAddressRole": "token" if f"{network}_{pool_address}" in {base_id, quote_id} else "pool",
    }


def _fetch_best_token_pool(
    client: httpx.Client,
    network: str,
    token_address: str,
    requested_symbol: str,
) -> tuple[str, dict[str, str]]:
    try:
        payload = _json_get(
            client,
            f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}/pools",
            {"include": "base_token,quote_token,dex", "page": 1},
        )
    except RuntimeError as exc:
        if "404" in str(exc):
            raise RuntimeError(f"{DEX_NETWORKS[network]} 未找到该池地址或代币合约地址") from exc
        raise
    token_resource_id = f"{network}_{token_address}"
    included = {
        item.get("id"): (item.get("attributes") or {})
        for item in (payload or {}).get("included") or []
        if isinstance(item, dict)
    }
    actual_symbol = str((included.get(token_resource_id) or {}).get("symbol") or "").upper()
    if actual_symbol and actual_symbol != requested_symbol:
        raise RuntimeError(f"该代币合约识别为 {actual_symbol}，不是 {requested_symbol}")

    candidates: list[tuple[float, dict[str, Any], str]] = []
    for item in (payload or {}).get("data") or []:
        if not isinstance(item, dict):
            continue
        relationships = item.get("relationships") or {}
        base_id = (((relationships.get("base_token") or {}).get("data") or {}).get("id"))
        quote_id = (((relationships.get("quote_token") or {}).get("data") or {}).get("id"))
        if token_resource_id == base_id:
            token_side = "base"
        elif token_resource_id == quote_id:
            token_side = "quote"
        else:
            continue
        attributes = item.get("attributes") or {}
        try:
            reserve_usd = float(attributes.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            reserve_usd = 0
        attributes = {**attributes, "poolDex": str((((relationships.get("dex") or {}).get("data") or {}).get("id")) or "未知池来源")}
        candidates.append((reserve_usd, attributes, token_side))
    if not candidates:
        raise RuntimeError(f"{requested_symbol} 暂未找到可读取历史K线的 DEX 池")
    _reserve_usd, attributes, token_side = max(candidates, key=lambda item: item[0])
    pool_address = str(attributes.get("address") or "").strip()
    if not pool_address:
        raise RuntimeError(f"{requested_symbol} 自动选择的 DEX 池缺少地址")
    return pool_address, {
        "tokenSide": token_side,
        "poolName": str(attributes.get("name") or requested_symbol),
        "poolDex": attributes.get("poolDex", "未知池来源"),
        "baseSymbol": "",
        "quoteSymbol": "",
    }


def _fetch_binance_rows(
    client: httpx.Client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor = start_ms
    max_pages = min(12, max(2, math.ceil(max(1, end_ms - start_ms) / interval_ms / 1500) + 1))
    for _page in range(max_pages):
        if cursor > end_ms:
            break
        try:
            payload = _json_get(
                client,
                "https://fapi.binance.com/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": 1500, "startTime": cursor, "endTime": end_ms},
            )
        except RuntimeError as exc:
            if "返回 400" in str(exc) or "Invalid symbol" in str(exc):
                raise RuntimeError(
                    f"Binance 未找到 {symbol} 永续合约；请切换到实际支持该合约的交易所"
                ) from exc
            raise
        if not isinstance(payload, list) or not payload:
            break
        last_ms = cursor
        for item in payload:
            if not isinstance(item, list) or len(item) < 5:
                continue
            timestamp_ms = int(item[0])
            close = float(item[4])
            if math.isfinite(close) and close > 0:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
                last_ms = max(last_ms, timestamp_ms)
        if last_ms <= cursor or last_ms + interval_ms > end_ms:
            break
        cursor = last_ms + interval_ms
    if len(rows) < 2:
        raise RuntimeError(f"Binance {symbol} 历史 K 线不足")
    return rows


def _fetch_aster_rows(
    client: httpx.Client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor = start_ms
    max_pages = min(12, max(2, math.ceil(max(1, end_ms - start_ms) / interval_ms / 1500) + 1))
    for _page in range(max_pages):
        if cursor > end_ms:
            break
        payload = _json_get(
            client,
            "https://fapi.asterdex.com/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": 1500, "startTime": cursor, "endTime": end_ms},
        )
        if not isinstance(payload, list) or not payload:
            break
        last_ms = cursor
        for item in payload:
            if not isinstance(item, list) or len(item) < 5:
                continue
            timestamp_ms = int(item[0])
            close = float(item[4])
            if math.isfinite(close) and close > 0:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
                last_ms = max(last_ms, timestamp_ms)
        if last_ms <= cursor or last_ms + interval_ms > end_ms:
            break
        cursor = last_ms + interval_ms
    if len(rows) < 2:
        raise RuntimeError(f"Aster {symbol} 历史 K 线不足")
    return rows


def _fetch_bybit_rows(
    client: httpx.Client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor_end = end_ms
    bybit_interval = interval.removesuffix("m")
    for _page in range(12):
        payload = _json_get(
            client,
            "https://api.bybit.com/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": bybit_interval,
                "start": start_ms,
                "end": cursor_end,
                "limit": 1000,
            },
        )
        page_rows = ((payload or {}).get("result") or {}).get("list") or []
        if not page_rows:
            break
        oldest_ms: int | None = None
        for item in page_rows:
            if not isinstance(item, list) or len(item) < 5:
                continue
            timestamp_ms = int(item[0])
            close = float(item[4])
            if math.isfinite(close) and close > 0:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
                oldest_ms = timestamp_ms if oldest_ms is None else min(oldest_ms, timestamp_ms)
        if oldest_ms is None or oldest_ms <= start_ms or oldest_ms >= cursor_end:
            break
        cursor_end = oldest_ms - 1
    if len(rows) < 2:
        raise RuntimeError(f"Bybit {symbol} 历史 K 线不足")
    return rows


def _fetch_bitget_rows(
    client: httpx.Client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor_end = end_ms
    for _page in range(12):
        payload = _json_get(
            client,
            "https://api.bitget.com/api/v2/mix/market/candles",
            {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "granularity": interval,
                "startTime": start_ms,
                "endTime": cursor_end,
                "limit": 1000,
            },
        )
        page_rows = (payload or {}).get("data") or []
        if not page_rows:
            break
        oldest_ms: int | None = None
        for item in page_rows:
            if not isinstance(item, list) or len(item) < 5:
                continue
            timestamp_ms = int(item[0])
            close = float(item[4])
            if math.isfinite(close) and close > 0:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
                oldest_ms = timestamp_ms if oldest_ms is None else min(oldest_ms, timestamp_ms)
        if oldest_ms is None or oldest_ms <= start_ms or oldest_ms >= cursor_end:
            break
        cursor_end = oldest_ms - 1
    if len(rows) < 2:
        raise RuntimeError(f"Bitget {symbol} 历史 K 线不足")
    return rows


def _fetch_okx_rows(
    client: httpx.Client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor_end = end_ms
    for _page in range(12):
        payload = _json_get(
            client,
            "https://www.okx.com/api/v5/market/history-candles",
            {"instId": symbol, "bar": interval, "after": cursor_end, "limit": 300},
        )
        page_rows = (payload or {}).get("data") or []
        if not page_rows:
            break
        oldest_ms: int | None = None
        for item in page_rows:
            if not isinstance(item, list) or len(item) < 5:
                continue
            timestamp_ms = int(item[0])
            close = float(item[4])
            if start_ms <= timestamp_ms <= end_ms and math.isfinite(close) and close > 0:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
            oldest_ms = timestamp_ms if oldest_ms is None else min(oldest_ms, timestamp_ms)
        if oldest_ms is None or oldest_ms <= start_ms or oldest_ms >= cursor_end:
            break
        cursor_end = oldest_ms
    if len(rows) < 2:
        raise RuntimeError(f"OKX {symbol} 历史 K 线不足")
    return rows


def _fetch_gate_rows(
    client: httpx.Client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
) -> dict[int, float]:
    rows: dict[int, float] = {}
    cursor = start_ms // 1000
    end_seconds = end_ms // 1000
    interval_seconds = interval_ms // 1000
    for _page in range(12):
        if cursor > end_seconds:
            break
        page_end = min(end_seconds, cursor + interval_seconds * 1999)
        payload = _json_get(
            client,
            "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
            {"contract": symbol, "interval": interval, "from": cursor, "to": page_end},
        )
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if isinstance(item, list) and len(item) >= 3:
                timestamp_ms, close = int(float(item[0]) * 1000), float(item[2])
            elif isinstance(item, dict):
                timestamp_ms, close = int(float(item.get("t", 0)) * 1000), float(item.get("c", 0))
            else:
                continue
            if math.isfinite(close) and close > 0:
                rows[(timestamp_ms // interval_ms) * interval_ms] = close
        if page_end >= end_seconds:
            break
        cursor = page_end + interval_seconds
    if len(rows) < 2:
        raise RuntimeError(f"Gate {symbol} 历史 K 线不足")
    return rows


def align_spread_points(dex_rows: dict[int, float], futures_rows: dict[int, float]) -> list[dict[str, float | int]]:
    points: list[dict[str, float | int]] = []
    for timestamp in sorted(dex_rows.keys() & futures_rows.keys()):
        dex_close = dex_rows[timestamp]
        futures_close = futures_rows[timestamp]
        spread_pct = astro_symmetric_spread_pct(dex_close, futures_close)
        if spread_pct is None:
            continue
        points.append(
            {
                "timestamp": timestamp,
                "dexClose": dex_close,
                "futuresClose": futures_close,
                "spreadPct": spread_pct,
            }
        )
    return points


def _read_spread_history(
    pool_input: str = Query(alias="poolAddress"),
    dex_symbol: str = Query(alias="dexSymbol"),
    dex_network_input: str = Query(default="solana", alias="dexNetwork"),
    futures_venue: str = Query(default="bn", alias="futuresVenue"),
    futures_input: str = Query(alias="futuresSymbol"),
    range_hours: int = Query(default=168, alias="rangeHours"),
    futures_divisor: float = 1.0,
) -> dict[str, Any]:
    if range_hours not in SUPPORTED_RANGES:
        raise HTTPException(status_code=400, detail="时间范围只支持 4小时、1天、3天、7天和30天")
    try:
        dex_network = normalize_dex_network(dex_network_input)
        pool_address = normalize_pool_address(pool_input, dex_network)
        venue = normalize_futures_venue(futures_venue)
        base_symbol, futures_symbol = normalize_futures_symbol(futures_input, venue)
        display_symbol = dex_symbol.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(display_symbol):
            raise ValueError("DEX 币名只支持 1 至 32 位中文、英文字母、数字或点号")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not math.isfinite(futures_divisor) or futures_divisor <= 0 or futures_divisor > 1e12:
        raise HTTPException(status_code=400, detail="合约价格除数必须为正数")
    interval, interval_ms = _granularity(range_hours)
    # Only completed candles: an in-progress close is not historical evidence.
    end_ms = int(time.time() * 1000) // interval_ms * interval_ms - 1
    start_ms = end_ms - range_hours * 60 * 60 * 1000
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            try:
                pool_metadata = _fetch_dex_pool_metadata(client, pool_address, display_symbol, dex_network)
                resolved_pool_address = pool_address
                pool_selection_mode = "pool_direct"
                query_address_type = "pool"
                if pool_metadata.get("inputAddressRole") == "token":
                    resolved_pool_address, pool_metadata = _fetch_best_token_pool(client, dex_network, pool_address, display_symbol)
                    pool_selection_mode = "token_auto"
                    query_address_type = "token"
            except RuntimeError as exc:
                if "404" not in str(exc):
                    raise
                resolved_pool_address, pool_metadata = _fetch_best_token_pool(
                    client,
                    dex_network,
                    pool_address,
                    display_symbol,
                )
                pool_selection_mode = "token_auto"
                query_address_type = "token"
            pair = {
                "symbol": display_symbol,
                "displayName": display_symbol,
                "inputAddress": pool_address,
                "poolAddress": resolved_pool_address,
                "poolName": pool_metadata["poolName"],
                "poolDex": pool_metadata.get("poolDex", "未知池来源"),
                "futuresDivisor": futures_divisor,
                "poolSelectionMode": pool_selection_mode,
                "queryAddressType": query_address_type,
                "historyPriceMode": "primary_pool_proxy" if query_address_type == "token" else "direct_pool",
                "dexTokenSide": pool_metadata["tokenSide"],
                "dexNetwork": dex_network,
                "dexNetworkLabel": DEX_NETWORKS[dex_network],
                "futuresVenue": venue,
                "futuresVenueLabel": FUTURES_VENUES[venue],
                "futuresBaseSymbol": base_symbol,
                "futuresSymbol": futures_symbol,
            }
            fetch_futures = {"gt": _fetch_gate_rows, "bn": _fetch_binance_rows, "bg": _fetch_bitget_rows,
                "okx": _fetch_okx_rows, "aster": _fetch_aster_rows, "by": _fetch_bybit_rows}[venue]
            # At most four workers across on-demand history requests; no scanner work.
            dex_job = _history_workers.submit(_fetch_dex_rows, client, pair, start_ms, end_ms, interval, interval_ms)
            future_job = _history_workers.submit(fetch_futures, client, futures_symbol, start_ms, end_ms, interval, interval_ms)
            try:
                dex_rows = dex_job.result()
                futures_rows = future_job.result()
            finally:
                # Keep the shared HTTP client alive until both readers finish, including failure.
                for job in (dex_job, future_job):
                    try:
                        job.result()
                    except Exception:
                        pass
            dex_rows = {t: v for t, v in dex_rows.items() if start_ms <= t <= end_ms}
            futures_rows = {t: v / futures_divisor for t, v in futures_rows.items() if start_ms <= t <= end_ms}
        points = align_spread_points(dex_rows, futures_rows)
        if len(points) < 2:
            raise RuntimeError("两端历史 K 线没有足够的同时间点")
    except (RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    values = [float(point["spreadPct"]) for point in points]
    matched = len(points)
    possible = min(len(dex_rows), len(futures_rows))
    expected = max(1, range_hours * 60 * 60 * 1000 // interval_ms)
    return {
        "observedAt": datetime.now(UTC).isoformat(),
        "pair": pair,
        "rangeHours": range_hours,
        "granularity": interval,
        "timezone": "UTC",
        "displayTimezone": "Asia/Shanghai",
        "sources": {
            "dex": f"GeckoTerminal · {pair['dexNetworkLabel']} · {pair['poolDex']} · {pair['poolName']}",
            "futures": f"{pair['futuresVenueLabel']} · {pair['futuresSymbol']}",
        },
        "spreadDefinition": {
            "rule": "astro_symmetric",
            "buyLeg": "dex",
            "sellLeg": "futures",
            "priceBasis": "aligned_kline_close",
            "formula": "2 × (右侧永续收盘价 - 左侧DEX收盘价) ÷ (右侧永续收盘价 + 左侧DEX收盘价) × 100%",
            "positiveMeaning": "右侧永续高于左侧DEX；方向对应买DEX、卖永续",
            "negativeMeaning": "右侧永续低于左侧DEX；方向对应买永续、卖DEX",
            "executionDifference": "历史采用同时间已结束K线，DEX为池内USD参考价，合约为USDT价；不代表OKX聚合路由或Pancake实际成交报价",
        },
        "dataPolicy": (
            "输入地址作为代币身份；DEX历史价格取该代币当前流动性最高池的云端K线；币种身份可从Astro读取；历史行情不使用本地成交数据"
            if pair["queryAddressType"] == "token"
            else "按指定DEX池直读云端历史K线；币种身份可从Astro读取；历史行情不使用本地成交数据"
        ),
        "points": points,
        "stats": {
            "latest": values[-1],
            "high": max(values),
            "low": min(values),
            "average": sum(values) / len(values),
            "pointCount": matched,
            "coveragePct": min(100, matched / expected * 100),
            "matchingPct": matched / possible * 100 if possible else 0,
            "expectedPointCount": expected,
            "dexPointCount": len(dex_rows),
            "futuresPointCount": len(futures_rows),
        },
    }


# Short cache shared by both DEX entry points when the pool/query is identical.
_history_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dex-history")
_cache_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
_inflight: dict[tuple, Future] = {}

def _cached_read(key: tuple, loader, ttl: float = 30.0):
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            _cache.move_to_end(key)
            return cached[1]
        job = _inflight.get(key)
        owner = job is None
        if owner:
            if len(_inflight) >= 4:
                raise HTTPException(status_code=429, detail="历史查询繁忙，请稍后再试")
            job = _inflight[key] = Future()
    if not owner:
        return job.result()
    try:
        result = loader()
        with _cache_lock:
            _cache[key] = (time.monotonic() + ttl, result)
            _cache.move_to_end(key)
            while len(_cache) > 64:
                _cache.popitem(last=False)
        job.set_result(result)
        return result
    except BaseException as exc:
        job.set_exception(exc)
        raise
    finally:
        with _cache_lock:
            _inflight.pop(key, None)


@router.get("")
def dex_spread_history(
    pool_input: str = Query(alias="poolAddress"),
    dex_symbol: str = Query(alias="dexSymbol"),
    dex_network_input: str = Query(default="solana", alias="dexNetwork"),
    futures_venue: str = Query(default="bn", alias="futuresVenue"),
    futures_input: str = Query(alias="futuresSymbol"),
    range_hours: int = Query(default=24, alias="rangeHours"),
    futures_divisor: float = Query(default=1, alias="futuresDivisor", gt=0, le=1e12),
) -> dict[str, Any]:
    try:
        network = normalize_dex_network(dex_network_input)
        address = normalize_pool_address(pool_input, network)
        venue = normalize_futures_venue(futures_venue)
        raw_symbol = normalize_futures_symbol(futures_input, venue)[0]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    key = ("history", network, address, dex_symbol.strip().upper(), venue, raw_symbol, range_hours, futures_divisor)
    return _cached_read(key, lambda: _read_spread_history(address, dex_symbol, network, venue, raw_symbol, range_hours, futures_divisor))


CHAIN_NETWORKS = {"501": "solana", "56": "bsc", "1": "eth", "8453": "base", "42161": "arbitrum", "4663": "robinhood"}


def _read_astro_cards():
    from app.astro_sdk import AstroSdkClient, astro_sdk_config, astro_sdk_read_total_seconds
    with AstroSdkClient(astro_sdk_config()) as client:
        return client.list_pairs(deadline=time.monotonic() + astro_sdk_read_total_seconds())


def _read_astro_coins():
    from app.astro_spread_scanner import spread_okxdex_quote_bridge_url
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(spread_okxdex_quote_bridge_url())
    url = urlunsplit((parts.scheme, parts.netloc, "/dex-coins", "", ""))
    with httpx.Client(timeout=5.0) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json().get("coins", [])


def build_route_catalog(cards, coins, mappings, records, aliases):
    from app.astro_spread_scanner import _direct_market_symbol
    assets = {}
    for source, rows in (("local", mappings), ("astro", coins)):
        for row in rows:
            chain = str(row.get("chainIndex") or "")
            network = CHAIN_NETWORKS.get(chain)
            symbol = str(row.get("symbol") or row.get("name") or "").strip().upper()
            if not network or not symbol:
                continue
            try:
                address = normalize_pool_address(str(row.get("contractAddress") or ""), network)
            except ValueError:
                continue
            key = f"{chain}:{address}"
            asset = assets.setdefault(key, {"id": key, "symbol": symbol, "symbols": [], "chainIndex": chain,
                "dexNetwork": network, "chainLabel": DEX_NETWORKS[network], "contractAddress": address, "sources": []})
            if symbol not in asset["symbols"]:
                asset["symbols"].append(symbol)
            if source not in asset["sources"]:
                asset["sources"].append(source)
    record_by_id = {str(r.get("astroPairId")): r for r in records if r.get("astroPairId")}
    routes = []
    for card in cards:
        buy = str(card.get("buyEx") or "").lower()
        sell = str(card.get("sellEx") or "").lower()
        if card.get("type") != "SF" or buy not in {"okxdex", "pancakeswapv3"} or sell not in FUTURES_VENUE_ALIASES:
            continue
        symbol = str(card.get("name") or "").strip().upper()
        options = [a for a in assets.values() if symbol in a["symbols"]]
        # Current Astro coin config takes priority over older local mappings; ambiguity remains explicit.
        configured = [a for a in options if "astro" in a["sources"]]
        options = configured or options
        record = record_by_id.get(str(card.get("id"))) or {}
        exact = [a for a in options if a["chainIndex"] == str(record.get("dexChainIndex")) and
            a["contractAddress"] == (str(record.get("dexContractAddress") or "").strip() if a["chainIndex"] == "501" else str(record.get("dexContractAddress") or "").strip().lower())]
        if exact:
            options = exact
        raw, divisor = _direct_market_symbol(sell, "future", symbol, aliases)
        def threshold(field):
            try:
                value = float(card[field]) * 100
                return value if math.isfinite(value) else None
            except (KeyError, TypeError, ValueError):
                return None
        routes.append({"id": str(card.get("id") or ""), "symbol": symbol, "buyExchange": buy, "sellExchange": sell,
            "futuresVenue": FUTURES_VENUE_ALIASES[sell], "futuresSymbol": raw, "futuresDivisor": divisor,
            "openTargetPct": threshold("openPosition"), "closeTargetPct": threshold("closePosition"),
            "assetIds": [a["id"] for a in options], "resolution": "resolved" if len(options) == 1 else "ambiguous" if options else "missing"})
    from app.book_spread_history import build_book_cards
    return {"assets": sorted(assets.values(), key=lambda a: (a["symbol"], a["chainIndex"])), "cards": routes,
            "bookCards": build_book_cards(cards, aliases)}


def _read_route_catalog():
    from app.astro_spread_scanner import spread_scan_dex_mapped_assets, _load_pulse_symbol_aliases
    from app.astro_card_registry import auto_created_route_records
    warnings = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        card_job = pool.submit(_read_astro_cards)
        coin_job = pool.submit(_read_astro_coins)
        try:
            cards = card_job.result()
        except Exception:
            cards = []
            warnings.append("Astro 卡片暂时读不到，可使用已配置币种或手动填写")
        try:
            coins = coin_job.result()
        except Exception:
            coins = []
            warnings.append("Astro 链地址暂时读不到，仅显示本地已有映射")
    result = build_route_catalog(cards, coins, spread_scan_dex_mapped_assets(), auto_created_route_records(), _load_pulse_symbol_aliases())
    return {**result, "warnings": warnings, "observedAt": datetime.now(UTC).isoformat(), "cacheSeconds": 30,
        "networks": [{"chainIndex": chain, "value": network, "label": DEX_NETWORKS[network]} for chain, network in CHAIN_NETWORKS.items()]}


@router.get("/routes")
def dex_history_routes():
    # Metadata reads do not confirm scanner mappings or mutate any Astro card.
    return _cached_read(("routes",), _read_route_catalog)


from app.book_spread_history import router as book_history_router
router.include_router(book_history_router)

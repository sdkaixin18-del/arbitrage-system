"""Read-only exchange relay. No credentials, subscriptions, cards or orders."""
from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

SERVICE = "astro-depth-cloud"
PATHS = {
    "api.binance.com": {"/api/v3/depth"},
    "fapi.binance.com": {"/fapi/v1/depth"},
    "fapi.asterdex.com": {"/fapi/v1/depth"},
    "api.bybit.com": {"/v5/market/orderbook"},
    "api.bitget.com": {"/api/v2/spot/market/orderbook", "/api/v2/mix/market/orderbook"},
    "www.okx.com": {"/api/v5/market/books", "/api/v5/public/instruments"},
    "api.gateio.ws": {"/api/v4/spot/order_book", "/api/v4/futures/usdt/order_book"},
}
PARAMETERS = {"symbol", "limit", "category", "type", "productType", "instId", "sz", "currency_pair", "contract", "with_id", "instType"}
_slots = threading.BoundedSemaphore(6)
_lock = threading.Lock()
_venue_slots: dict[str, threading.BoundedSemaphore] = defaultdict(lambda: threading.BoundedSemaphore(2))
_backoff: dict[str, float] = {}
_last_request: dict[str, float] = {}
_client = httpx.Client(timeout=httpx.Timeout(3.0, connect=1.5), trust_env=False, follow_redirects=False)
_dex_lock = threading.Lock()
_last_dex_request = 0.0
_dex_config_lock = threading.Lock()
_dex_config_cache: tuple[float, list[dict[str, Any]]] | None = None

DEX_COIN_FILE = Path(os.environ.get("ASTRO_DEX_COIN_FILE", "/var/lib/astro-depth-cloud/dex-coins.json"))
OKX_CREDENTIAL_FILE = Path(os.environ.get("ASTRO_OKX_CREDENTIAL_FILE", "/etc/astro-depth-cloud/okx-dex.json"))
OKX_QUOTE_PATH = "/api/v6/dex/aggregator/quote"
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
PANCAKE_CHAINS = {
    "1": {
        "rpc": "https://ethereum-rpc.publicnode.com",
        "quoter": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "quote": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "quoteDecimals": 6,
        "nativeTicker": "ETHUSDT",
    },
    "56": {
        "rpc": "https://bsc-dataseed.bnbchain.org",
        "quoter": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "quote": "0x55d398326f99059ff775485246999027b3197955",
        "quoteDecimals": 18,
        "nativeTicker": "BNBUSDT",
    },
    "8453": {
        "rpc": "https://mainnet-preconf.base.org",
        "quoter": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "quote": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "quoteDecimals": 6,
        "nativeTicker": "ETHUSDT",
    },
    "42161": {
        "rpc": "https://arb1.arbitrum.io/rpc",
        "quoter": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "quote": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
        "quoteDecimals": 6,
        "nativeTicker": "ETHUSDT",
    },
}


@asynccontextmanager
async def lifespan(_app):
    # Warm only Python adapters (no network requests or background scans).
    from app import crypto  # noqa: F401
    yield
    _client.close()


app = FastAPI(title=SERVICE, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class PublicGet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=240)
    params: dict[str, str | int] = Field(default_factory=dict)


def validate_target(request: PublicGet) -> str:
    url = urlsplit(request.url)
    if url.scheme != "https" or url.port not in (None, 443) or url.username or url.password or url.query or url.fragment:
        raise HTTPException(400, "Only fixed public HTTPS endpoints are allowed")
    allowed = url.path in PATHS.get(url.hostname or "", set())
    if url.hostname == "api.gateio.ws" and re.fullmatch(r"/api/v4/futures/usdt/contracts/[A-Z0-9\u4e00-\u9fff]{1,40}_USDT", url.path):
        allowed = True
    if not allowed or set(request.params) - PARAMETERS:
        raise HTTPException(400, "Endpoint or parameters not allowed")
    for key, value in request.params.items():
        if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff-]{1,60}", str(value)):
            raise HTTPException(400, "Invalid public parameter")
        if key in {"limit", "sz"} and str(value) not in {"5", "20"}:
            raise HTTPException(400, "Only 5/20 levels are allowed")
    return url.hostname or ""


@app.get("/health")
def health():
    return {
        "service": SERVICE,
        "protocol": 1,
        "readOnly": True,
        "serverTimeMs": int(time.time() * 1000),
        "maxConcurrency": 6,
        "dexQuote": {
            "okxdex": OKX_CREDENTIAL_FILE.is_file(),
            "pancakeswapv3": True,
        },
    }


def _load_dex_coins() -> list[dict[str, Any]]:
    global _dex_config_cache
    try:
        modified = DEX_COIN_FILE.stat().st_mtime
    except OSError as exc:
        raise HTTPException(503, "DEX coin configuration unavailable") from exc
    with _dex_config_lock:
        if _dex_config_cache and _dex_config_cache[0] == modified:
            return _dex_config_cache[1]
        try:
            payload = json.loads(DEX_COIN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "DEX coin configuration invalid") from exc
        if not isinstance(payload, list):
            raise HTTPException(503, "DEX coin configuration invalid")
        coins = [item for item in payload if isinstance(item, dict)]
        _dex_config_cache = (modified, coins)
        return coins


def _configured_coin(symbol: str, chain_index: str, contract_address: str) -> dict[str, Any]:
    normalized_contract = contract_address.lower()
    for coin in _load_dex_coins():
        if (
            str(coin.get("name") or "").strip().upper() == symbol
            and str(coin.get("chainIndex") or "").strip() == chain_index
            and str(coin.get("contractAddress") or "").strip().lower() == normalized_contract
        ):
            return coin
    raise HTTPException(404, "DEX coin is not configured with this exact chain and contract")


def _decimal_string(raw_value: Any, decimals_value: Any) -> str:
    raw = str(raw_value or "").strip()
    try:
        decimals = int(decimals_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(502, "DEX returned invalid token decimals") from exc
    if not raw.isdigit() or not 0 <= decimals <= 36:
        raise HTTPException(502, "DEX returned invalid token amount")
    padded = raw.zfill(decimals + 1)
    whole = padded[:-decimals] if decimals else padded
    fraction = padded[-decimals:].rstrip("0") if decimals else ""
    return f"{whole}.{fraction}" if fraction else whole


def _quote_amount_raw(amount_usdt: float, decimals: int) -> str:
    try:
        value = Decimal(str(amount_usdt)) * (Decimal(10) ** decimals)
    except InvalidOperation as exc:
        raise HTTPException(400, "Invalid DEX quote amount") from exc
    integral = value.to_integral_value()
    if value != integral:
        raise HTTPException(400, "DEX quote amount has too many decimal places")
    return str(int(integral))


def _read_okx_credentials() -> dict[str, str]:
    try:
        payload = json.loads(OKX_CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "OKX DEX quote credentials unavailable") from exc
    if not isinstance(payload, dict) or not all(str(payload.get(key) or "") for key in ("K", "S", "P")):
        raise HTTPException(503, "OKX DEX quote credentials invalid")
    return {key: str(payload[key]) for key in ("K", "S", "P")}


def _okx_quote(symbol: str, chain_index: str, contract_address: str, amount_usdt: float) -> dict[str, Any]:
    credentials = _read_okx_credentials()
    quote_tokens = {chain: config for chain, config in PANCAKE_CHAINS.items()}
    quote = quote_tokens.get(chain_index)
    if not quote:
        if chain_index == "501":
            quote = {"quote": "Es9vMFrzaCERmJfrF4H2FYDkC6WQ5GQ7gGzQfX3b1qB", "quoteDecimals": 6}
        else:
            raise HTTPException(422, "USDT quote token is not configured for this chain")
    params = {
        "chainIndex": chain_index,
        "amount": _quote_amount_raw(amount_usdt, int(quote["quoteDecimals"])),
        "fromTokenAddress": str(quote["quote"]),
        "toTokenAddress": contract_address,
        "swapMode": "exactIn",
    }
    request_path = f"{OKX_QUOTE_PATH}?{urlencode(params)}"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    signature = base64.b64encode(
        hmac.new(credentials["S"].encode(), f"{timestamp}GET{request_path}".encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": credentials["K"],
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-PASSPHRASE": credentials["P"],
        "OK-ACCESS-TIMESTAMP": timestamp,
    }
    try:
        response = _client.get(f"https://web3.okx.com{request_path}", headers=headers, timeout=6.0)
        payload = response.json()
    except (httpx.TransportError, ValueError) as exc:
        raise HTTPException(502, "OKX DEX quote transport unavailable") from exc
    if response.status_code != 200 or not isinstance(payload, dict) or str(payload.get("code")) != "0":
        message = str(payload.get("msg") or f"HTTP {response.status_code}") if isinstance(payload, dict) else f"HTTP {response.status_code}"
        raise HTTPException(502, f"OKX DEX quote rejected: {message[:160]}")
    rows = payload.get("data")
    raw = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if not raw:
        raise HTTPException(502, "OKX DEX returned no executable route")
    from_token = raw.get("fromToken") if isinstance(raw.get("fromToken"), dict) else {}
    to_token = raw.get("toToken") if isinstance(raw.get("toToken"), dict) else {}
    from_decimals = int(from_token.get("decimal") or quote["quoteDecimals"])
    to_decimals = int(to_token.get("decimal"))
    dexes = []
    for route in raw.get("dexRouterList") or []:
        protocol = route.get("dexProtocol") if isinstance(route, dict) else None
        name = str(protocol.get("dexName") or "").strip() if isinstance(protocol, dict) else ""
        if name and name not in dexes:
            dexes.append(name)
    return {
        "source": "okx_v6_quote_api",
        "exchange": "okxdex",
        "quotedAt": int(time.time() * 1000),
        "symbol": symbol,
        "chainIndex": chain_index,
        "contractAddress": contract_address,
        "amountUsdt": amount_usdt,
        "fromTokenAddress": str(from_token.get("tokenContractAddress") or quote["quote"]),
        "toTokenAddress": str(to_token.get("tokenContractAddress") or contract_address),
        "fromDecimals": from_decimals,
        "toDecimals": to_decimals,
        "fromAmount": _decimal_string(raw.get("fromTokenAmount"), from_decimals),
        "toAmount": _decimal_string(raw.get("toTokenAmount"), to_decimals),
        "tradeFeeUsd": str(raw.get("tradeFee") or "0"),
        "estimateGasFee": str(raw.get("estimateGasFee") or ""),
        "priceImpactPercent": None if raw.get("priceImpactPercent") is None else str(raw.get("priceImpactPercent")),
        "buyTaxRate": None if to_token.get("taxRate") is None else str(to_token.get("taxRate")),
        "quoteId": str(raw.get("quoteId") or ""),
        "routeDexes": dexes,
    }


def _word(value: int) -> str:
    return f"{value:064x}"


def _address_word(value: str) -> str:
    return value.lower().removeprefix("0x").zfill(64)


def _rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    try:
        response = _client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=4.0)
        payload = response.json()
    except (httpx.TransportError, ValueError) as exc:
        raise HTTPException(502, "PancakeSwap RPC unavailable") from exc
    if response.status_code != 200 or not isinstance(payload, dict) or payload.get("error") or payload.get("result") is None:
        raise HTTPException(502, "PancakeSwap RPC rejected the read-only quote")
    return payload["result"]


def _pancake_quote(symbol: str, chain_index: str, contract_address: str, amount_usdt: float) -> dict[str, Any]:
    config = PANCAKE_CHAINS.get(chain_index)
    if not config or not EVM_ADDRESS.fullmatch(contract_address):
        raise HTTPException(422, "PancakeSwap chain is unavailable")
    rpc = str(config["rpc"])
    decimals_result = _rpc_call(rpc, "eth_call", [{"to": contract_address, "data": "0x313ce567"}, "latest"])
    try:
        token_decimals = int(str(decimals_result), 16)
    except ValueError as exc:
        raise HTTPException(502, "PancakeSwap token decimals invalid") from exc
    amount_raw = int(_quote_amount_raw(amount_usdt, int(config["quoteDecimals"])))
    quotes: list[tuple[int, int, int]] = []
    for fee in (100, 500, 2500, 10000):
        data = "0xc6a5026a" + "".join((
            _address_word(str(config["quote"])),
            _address_word(contract_address),
            _word(amount_raw),
            _word(fee),
            _word(0),
        ))
        try:
            result = str(_rpc_call(rpc, "eth_call", [{"to": str(config["quoter"]), "data": data}, "latest"]))
            words = [result[2 + offset:2 + offset + 64] for offset in range(0, len(result) - 2, 64)]
            if len(words) >= 4:
                output, gas = int(words[0], 16), int(words[3], 16)
                if output > 0:
                    quotes.append((output, gas, fee))
        except HTTPException:
            continue
    if not quotes:
        raise HTTPException(502, "PancakeSwap has no executable direct USDT V3 pool")
    output, gas, fee = max(quotes, key=lambda item: item[0])
    gas_price = int(str(_rpc_call(rpc, "eth_gasPrice", [])), 16)
    try:
        ticker_response = _client.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": config["nativeTicker"]},
            timeout=4.0,
        )
        native_price = Decimal(str(ticker_response.json()["price"]))
    except (httpx.TransportError, ValueError, KeyError, InvalidOperation) as exc:
        raise HTTPException(502, "PancakeSwap native token price unavailable") from exc
    gas_units = gas + 150_000
    gas_usd = Decimal(gas_units * gas_price) / (Decimal(10) ** 18) * native_price
    return {
        "source": "pancakeswap_v3_quoter_eth_call",
        "exchange": "pancakeswapv3",
        "quotedAt": int(time.time() * 1000),
        "symbol": symbol,
        "chainIndex": chain_index,
        "contractAddress": contract_address,
        "amountUsdt": amount_usdt,
        "fromAmount": str(amount_usdt),
        "toAmount": _decimal_string(output, token_decimals),
        "tradeFeeUsd": str(gas_usd),
        "networkFeeEstimated": True,
        "estimateGasFee": str(gas_units),
        "feeTier": fee,
        "routeDexes": ["PancakeSwap V3"],
        "quoteId": "",
        "fromTokenAddress": str(config["quote"]),
        "toTokenAddress": contract_address,
    }


@app.get("/dex-coins")
def dex_coins():
    return {"coins": [
        {
            "name": str(coin.get("name") or ""),
            "chainIndex": str(coin.get("chainIndex") or ""),
            "contractAddress": str(coin.get("contractAddress") or ""),
        }
        for coin in _load_dex_coins()
    ]}


@app.get("/dex-quote")
@app.get("/v1/dex-quote")
def dex_quote(symbol: str, chainIndex: str, contractAddress: str, amountUsdt: float, exchange: str = "okxdex"):
    normalized_symbol = symbol.strip().upper()
    normalized_exchange = exchange.strip().lower()
    normalized_chain = chainIndex.strip()
    normalized_contract = contractAddress.strip()
    if not re.fullmatch(r"[A-Z0-9\u4e00-\u9fff._-]{1,32}", normalized_symbol):
        raise HTTPException(400, "Invalid DEX symbol")
    if not re.fullmatch(r"\d{1,8}", normalized_chain) or not normalized_contract or len(normalized_contract) > 128:
        raise HTTPException(400, "Invalid DEX chain or contract")
    if not math.isfinite(amountUsdt) or not 0 < amountUsdt <= 10_000:
        raise HTTPException(400, "Invalid DEX quote amount")
    # The local mapping page is the source of truth for newly added assets.
    # This relay stays quote-only and therefore validates identity syntax here
    # without turning a periodically copied Astro coin list into a stale gate.
    if normalized_chain == "501":
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", normalized_contract):
            raise HTTPException(400, "Invalid Solana token address")
    elif not EVM_ADDRESS.fullmatch(normalized_contract):
        raise HTTPException(400, "Invalid EVM token address")
    global _last_dex_request
    with _dex_lock:
        wait = max(0.0, 1.1 - (time.monotonic() - _last_dex_request))
        if wait:
            time.sleep(wait)
        _last_dex_request = time.monotonic()
        if normalized_exchange == "okxdex":
            return _okx_quote(normalized_symbol, normalized_chain, normalized_contract, amountUsdt)
        if normalized_exchange == "pancakeswapv3":
            return _pancake_quote(normalized_symbol, normalized_chain, normalized_contract, amountUsdt)
        raise HTTPException(400, "Unsupported DEX quote venue")


@app.post("/v1/public-get")
def public_get(request: PublicGet):
    host = validate_target(request)
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "Cloud depth worker busy", headers={"Retry-After": "1"})
    venue_slot = _venue_slots[host]
    acquired = False
    try:
        acquired = venue_slot.acquire(timeout=0.5)
        if not acquired:
            raise HTTPException(429, "Exchange concurrency limit", headers={"Retry-After": "1"})
        with _lock:
            now = time.monotonic()
            if now < _backoff.get(host, 0):
                raise HTTPException(429, "Exchange rate backoff", headers={"Retry-After": str(math.ceil(_backoff[host] - now))})
            scheduled = max(now, _last_request.get(host, 0) + 0.1)
            _last_request[host] = scheduled
        time.sleep(max(0, scheduled - time.monotonic()))
        started = int(time.time() * 1000)
        response = _client.get(request.url, params=request.params)
        received = int(time.time() * 1000)
        if response.status_code in {418, 429}:
            try:
                delay = max(1, min(300, float(response.headers.get("retry-after", "5"))))
            except ValueError:
                delay = 5
            with _lock:
                _backoff[host] = time.monotonic() + delay
        try:
            payload = response.json()
        except ValueError:
            raise HTTPException(502, "Exchange returned invalid JSON")
        return {"service": SERVICE, "protocol": 1, "url": request.url,
                "statusCode": response.status_code, "payload": payload,
                "receivedAtMs": received, "requestStartedAtMs": started,
                "retryAfter": response.headers.get("retry-after")}
    except httpx.TransportError as exc:
        raise HTTPException(502, f"Exchange transport unavailable: {type(exc).__name__}") from exc
    finally:
        if acquired:
            venue_slot.release()
        _slots.release()


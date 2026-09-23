"""Bounded, read-only contract lifecycle checks independent of news watches."""
from concurrent.futures import ThreadPoolExecutor
import math
import threading
import time

URL = "https://api.bitget.com/api/v2/mix/market/contracts"
MAX_AGE_SECONDS = 300
_lock = threading.RLock()
_executor = None
_future = None
_rows = {}
_fetched_at = 0.0
_last_attempt = 0.0
_error = None


def parse_bitget_contracts(payload):
    if not isinstance(payload, dict) or payload.get("code") != "00000" or not isinstance(payload.get("data"), list) or not payload["data"]:
        raise ValueError("invalid_bitget_contract_inventory")
    result = {}
    for row in payload["data"]:
        if not isinstance(row, dict) or row.get("quoteCoin") != "USDT":
            continue
        symbol = str(row.get("baseCoin") or row.get("symbol", "").removesuffix("USDT")).upper()
        if not symbol:
            continue
        def stamp(name):
            try:
                value = float(row.get(name) or -1)
                return int(value) if math.isfinite(value) and value > 0 else None
            except (TypeError, ValueError):
                return None
        result[symbol] = {"symbol": symbol, "exchange": "bitget", "market": "future",
                          "status": str(row.get("symbolStatus") or "").lower(),
                          "offTimeMs": stamp("offTime"), "limitOpenTimeMs": stamp("limitOpenTime"),
                          "source": "exchange_contract_api", "sourceUrl": URL}
    if not result:
        raise ValueError("empty_bitget_contract_inventory")
    return result


def _fetch():
    import httpx
    from app.crypto import api_rate_limit_lane, api_request_deadline, request_json
    with api_rate_limit_lane("astro_evidence"), api_request_deadline(8), httpx.Client(timeout=3) as client:
        return parse_bitget_contracts(request_json(client, URL, {"productType": "USDT-FUTURES"}))


def _completed(future):
    global _future, _rows, _fetched_at, _error
    with _lock:
        try:
            _rows = future.result()
            _fetched_at = time.time()
            _error = None
        except Exception as exc:
            _error = type(exc).__name__  # No proxy/authenticated URL in status.
        finally:
            _future = None


def prefetch():
    global _future, _executor, _last_attempt
    with _lock:
        now = time.time()
        if _future is not None or now - _fetched_at < MAX_AGE_SECONDS or now - _last_attempt < 10:
            return
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astro-contract-safety")
        _last_attempt = now
        _future = _executor.submit(_fetch)
        _future.add_done_callback(_completed)


def route_check(symbol, pair_type, buy_exchange, sell_exchange, *, now=None):
    if sell_exchange != "bitget" and not (pair_type == "FF" and buy_exchange == "bitget"):
        return None
    prefetch()
    with _lock:
        at = time.time() if now is None else now
        row = dict(_rows.get(str(symbol).upper().removesuffix("USDT"), {}))
        age = at - _fetched_at
    # A known lifecycle restriction remains a block during a transport failure.
    if row.get("offTimeMs") or row.get("limitOpenTimeMs"):
        return {**row, "decision": "reject", "reason": "contract_delisting_announced", "ageSeconds": age}
    if row.get("status") and row["status"] not in {"normal", "online", "tradable", "trading"}:
        return {**row, "decision": "reject", "reason": "contract_opening_restricted", "ageSeconds": age}
    if not row or not 0 <= age <= MAX_AGE_SECONDS or not row.get("status"):
        return {**row, "decision": "observe", "reason": "contract_status_pending", "exchange": "bitget"}
    return None


def status():
    with _lock:
        return {"sourceUrl": URL, "exchange": "bitget", "market": "future", "maxAgeSeconds": MAX_AGE_SECONDS,
                "fetchedAtMs": int(_fetched_at * 1000) if _fetched_at else None,
                "pending": _future is not None, "lastError": _error, "contractCount": len(_rows),
                "scheduledDelistingCount": sum(bool(row.get("offTimeMs") or row.get("limitOpenTimeMs")) for row in _rows.values()),
                "independentOfNewsPush": True}

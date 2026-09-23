"""Small on-demand SF funding gate. No background polling or profit forecasts."""
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_prefetch_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-funding")
_queued = set()


def prefetch(exchange, raw_symbol, fetch):
    """Bound candidate work and share the exact same cache as the final gate."""
    key = (exchange, raw_symbol)
    with _lock:
        cached = _cache.get(key)
        if key in _queued or key in _inflight or len(_queued) >= 16:
            return False
        if cached and time.monotonic() - cached[0] < cached[1].get("retryDelaySeconds", CACHE_SECONDS):
            return False
        _queued.add(key)
    def run():
        try:
            _read(exchange, raw_symbol, fetch)
        finally:
            with _lock:
                _queued.discard(key)
    try:
        _prefetch_pool.submit(run)
    except Exception:
        with _lock:
            _queued.discard(key)
        return False
    return True

_lock = threading.Lock()
_cache = {}
_inflight = set()
_pulse_negative_since = {}
CACHE_SECONDS = 10.0
EXEMPT_SPREAD_PCT = 2.5
PRECHECK_SPREAD_PCT = 2.3
PULSE_MAX_AGE_SECONDS = 15.0
PULSE_NEAR_ZERO_PCT = 0.001


def parse_rate(exchange, body):
    if exchange in {"binance", "aster"}:
        value = body.get("lastFundingRate")
    elif exchange == "bybit":
        if body.get("retCode") != 0:
            raise ValueError("funding API rejected request")
        value = body["result"]["list"][0].get("fundingRate")
    elif exchange in {"bitget", "okx"}:
        if str(body.get("code")) != ("00000" if exchange == "bitget" else "0"):
            raise ValueError("funding API rejected request")
        value = body["data"][0].get("fundingRate")
    elif exchange == "gate":
        value = body.get("funding_rate")
    else:
        raise ValueError("funding venue unsupported")
    # Missing must never be coerced into zero (zero itself is eligible).
    rate = float(value)
    if not math.isfinite(rate):
        raise ValueError("funding rate invalid")
    return rate * 100


def endpoint(exchange, raw_symbol):
    symbol = raw_symbol + "USDT"
    if exchange in {"binance", "aster"}:
        host = "fapi.binance.com" if exchange == "binance" else "fapi.asterdex.com"
        return f"https://{host}/fapi/v1/premiumIndex", {"symbol": symbol}
    if exchange == "bybit":
        return "https://api.bybit.com/v5/market/tickers", {"category": "linear", "symbol": symbol}
    if exchange == "bitget":
        return "https://api.bitget.com/api/v2/mix/market/current-fund-rate", {"symbol": symbol, "productType": "USDT-FUTURES"}
    if exchange == "okx":
        return "https://www.okx.com/api/v5/public/funding-rate", {"instId": raw_symbol + "-USDT-SWAP"}
    if exchange == "gate":
        return "https://api.gateio.ws/api/v4/futures/usdt/contracts/" + raw_symbol + "_USDT", {}
    raise ValueError("funding venue unsupported")


def check(pair_type, spread_pct, exchange, raw_symbol, fetch, *, background=False):
    if pair_type != "SF":
        return {"eligible": True, "status": "not_applicable", "read": False}
    if spread_pct >= EXEMPT_SPREAD_PCT:
        return {"eligible": True, "status": "spread_exempt", "read": False, "exemptSpreadPct": EXEMPT_SPREAD_PCT}
    if background:
        key = (exchange, raw_symbol)
        with _lock:
            cached = _cache.get(key)
            if cached:
                remaining = cached[1].get("retryDelaySeconds", CACHE_SECONDS) - (time.monotonic() - cached[0])
                if remaining > 0:
                    return {**cached[1], "cached": True, "read": False, "retryAfterSeconds": remaining}
        prefetch(exchange, raw_symbol, fetch)
        # No worker waits for network here; the next route check must fetch
        # fresh depth before it may consume the completed funding result.
        return {"eligible": False, "status": "funding_pending", "read": False, "retryAfterSeconds": 0.5}
    return _read(exchange, raw_symbol, fetch)


def precheck(pair_type, pulse_spread_pct, exchange, raw_symbol, *, pulse=None):
    """No network reads here. Pulse can briefly defer depth, never approve a card.

    A market timestamp is not a funding timestamp. Bound a Pulse-only negative
    wait so a stale upstream rate cannot indefinitely hide a valid opportunity.
    """
    if pair_type != "SF":
        return {"eligible": True, "status": "not_applicable", "read": False}
    if pulse_spread_pct is None or pulse_spread_pct >= PRECHECK_SPREAD_PCT:
        return {"eligible": True, "status": "check_executable_spread", "read": False}
    key = (exchange, raw_symbol)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached:
            remaining = cached[1].get("retryDelaySeconds", CACHE_SECONDS) - (now - cached[0])
            if remaining > 0:
                return {**cached[1], "cached": True, "read": False, "retryAfterSeconds": remaining}
        if key in _inflight:
            return {"eligible": False, "status": "funding_pending", "read": False, "retryAfterSeconds": 0.5}
    hint = pulse if isinstance(pulse, dict) else {}
    rate = stamp = None
    try:
        rate, stamp = float(hint.get("rawRate")), float(hint.get("marketAtMs"))
        if (not math.isfinite(rate) or not math.isfinite(stamp)
                or hint.get("rawSymbol") != raw_symbol
                or not -5 <= time.time() - stamp / 1000 <= PULSE_MAX_AGE_SECONDS):
            rate = None
    except (TypeError, ValueError):
        pass
    # Preserve negative zero for diagnostics. Near-zero, absent and stale data
    # proceeds to depth, followed by the unchanged exact API gate.
    info = {"source": "pulse_hint", "read": False, "pulseRateRaw": hint.get("rawRate"),
            "pulseNegativeSign": rate is not None and math.copysign(1, rate) < 0,
            "finalApiRequired": True}
    with _lock:
        if rate is None or rate >= -PULSE_NEAR_ZERO_PCT:
            _pulse_negative_since.pop(key, None)
            return {**info, "eligible": True, "status": "pulse_unknown" if rate is None else "pulse_candidate"}
        if len(_pulse_negative_since) >= 512 and key not in _pulse_negative_since:
            # A full hint cache must fail open to depth, not reset other waits.
            return {**info, "eligible": True, "status": "pulse_hint_capacity"}
        since = _pulse_negative_since.setdefault(key, now)
        remaining = CACHE_SECONDS - (now - since)
        if remaining <= 0:
            return {**info, "eligible": True, "status": "pulse_negative_recheck_due"}
        return {**info, "eligible": False, "status": "pulse_negative", "retryAfterSeconds": remaining}


def _read(exchange, raw_symbol, fetch):
    key = (exchange, raw_symbol)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached:
            remaining = cached[1].get("retryDelaySeconds", CACHE_SECONDS) - (now - cached[0])
            if remaining > 0:
                return {**cached[1], "cached": True, "read": False, "retryAfterSeconds": remaining}
        if key in _inflight:
            return {"eligible": False, "status": "funding_pending", "read": False, "retryAfterSeconds": 0.5}
        _inflight.add(key)
    try:
        url, params = endpoint(exchange, raw_symbol)
        rate = parse_rate(exchange, fetch(url, params))
        result = {"eligible": rate >= 0, "status": "non_negative" if rate >= 0 else "negative", "source": "exchange_api",
                  "ratePct": rate, "read": True, "checkedAtMs": int(time.time() * 1000),
                  "retryDelaySeconds": CACHE_SECONDS, "retryAfterSeconds": CACHE_SECONDS}
        with _lock:
            if len(_cache) >= 512:
                _cache.clear()
            _cache[key] = (time.monotonic(), result)
        return result
    except Exception as exc:
        failures = int((cached[1] if cached else {}).get("failureCount", 0)) + 1
        delay = (1.0, 3.0, 10.0, 30.0)[min(failures - 1, 3)]
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        category = ("rate_limited" if status_code == 429 else
                    "timeout" if "Timeout" in type(exc).__name__ else
                    "http_error" if status_code else
                    "invalid_or_missing_data" if isinstance(exc, (ValueError, TypeError, KeyError, IndexError)) else "connection_error")
        result = {"eligible": False, "status": "funding_unavailable", "read": True, "source": "exchange_api",
                  "errorCategory": category, "errorType": type(exc).__name__, "httpStatus": status_code,
                  "failureCount": failures, "retryDelaySeconds": delay, "retryAfterSeconds": delay}
        with _lock:
            if len(_cache) >= 512:
                _cache.clear()
            _cache[key] = (time.monotonic(), result)
        return result
    finally:
        with _lock:
            _inflight.discard(key)

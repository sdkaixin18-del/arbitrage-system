"""Saved Astro rules: cached display values, fresh fail-closed submission reads."""
import json
import math
import os
from pathlib import Path
import threading


class SettingsReadError(ValueError):
    pass


_lock = threading.RLock()
_last_good = {}
_signatures = {}


def settings_path():
    explicit = os.environ.get("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", "").strip()
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else (
        Path(data_dir).expanduser() / "astro-spread-subscriptions.json" if data_dir else None
    )


def read_settings(path=None, *, strict=False, allow_missing=False):
    path = settings_path() if path is None else path
    if path is None:
        return {}
    key = str(path.absolute())
    try:
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        # Cache validated JSON, not mutable dictionaries. Submission reads bypass it.
        # File I/O and decoding stay outside the lock to avoid starving status reads.
        with _lock:
            cached = _last_good.get(key) if not strict and _signatures.get(key) == signature else None
        if cached is not None:
            return json.loads(cached)
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("settings must be an object")
        for field in ("blockedCoins", "blockedPairs", "markets", "dexMappedAssets"):
            if field in payload and not isinstance(payload[field], list):
                raise ValueError(f"invalid {field}")
        if any(not isinstance(coin, str) or not coin.strip() or not coin.strip().isalnum()
               for coin in payload.get("blockedCoins", [])):
            raise ValueError("invalid blockedCoins entry")
        if any(not isinstance(row, dict) or not row.get("marketKey") or not (row.get("symbol") or row.get("coin"))
               for row in payload.get("blockedPairs", [])):
            raise ValueError("invalid blockedPairs entry")
        for field, value in payload.items():
            if field == "scanIntervalSeconds" and (isinstance(value, bool) or not 5 <= float(value) <= 60 or not float(value).is_integer()):
                raise ValueError(f"invalid {field}")
            if field == "ffBybitSellExceptionMinOpenSpreadPct" and (isinstance(value, bool) or not 0.01 <= float(value) <= 100):
                raise ValueError(f"invalid {field}")
            if (field.endswith("Enabled") or field in ("excludeDelistedExchangeCards", "priceChangeAlertOnlyRise")) and not isinstance(value, bool):
                raise ValueError(f"invalid {field}")
            if field.endswith(("Pct", "PctPoints", "Usdt")) or field in ("confirmations", "maxQuoteAgeSeconds"):
                if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
                    raise ValueError(f"invalid {field}")
        with _lock:
            _last_good[key] = raw
            _signatures[key] = signature
        return payload
    except (OSError, ValueError, TypeError) as exc:
        with _lock:
            cached = _last_good.get(key)
        if isinstance(exc, FileNotFoundError) and allow_missing and cached is None:
            return {}
        if strict:
            raise SettingsReadError(f"Astro saved rules unavailable ({type(exc).__name__})") from exc
        return json.loads(cached) if cached is not None else {}


def settings_health():
    try:
        read_settings(strict=True)
        return {"state": "ok", "newCardsAllowed": True}
    except SettingsReadError as exc:
        return {"state": "read_failed", "newCardsAllowed": False, "message": str(exc)}

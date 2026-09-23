from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from app.astro_card_registry import (
    AUTO_CARD_CONFIG_FIELDS,
    AUTO_CARD_SNAPSHOT_VERSION,
    inactive_unreadable_config_fields,
    clear_pending_astro_submission,
    pending_astro_submission_status,
    reconcile_pending_astro_submissions,
    record_pending_astro_submission,
    pending_submission_checks_due,
    record_submission_check,
    mark_submission_not_executed,
    record_submission_request,
    apply_delete_rearm_rules,
    check_delete_rearm_before_submit,
    astro_cleanup_status,
    mark_auto_card_system_deleted,
    observe_auto_card_cleanup,
    pair_identity,
    pair_lifecycle_identity,
    register_auto_created_pair,
    reset_auto_card_invalid_observation,
)
from app.system_runtime_log import append_system_runtime_event
from app.astro_sdk_budget import BudgetDeferred, budget_for
from app.astro_sdk_reads import ReadDeferred, list_reads
from app.astro_io_metrics import record as record_io, snapshot as io_snapshot
from app import astro_label_queue


ASTRO_EXCHANGE_NAMES = {
    "bn": "binance",
    "by": "bybit",
    "gt": "gate",
    "okx": "okx",
    "bg": "bitget",
    "htx": "htx",
    "as": "aster",
    "hl": "hl",
}
ASTRO_FF_BUY_EXCHANGES = frozenset({"binance", "bybit", "bitget", "okx", "gate", "aster"})
ASTRO_FF_SELL_EXCHANGES = frozenset({"binance", "bitget", "okx", "gate", "aster"})
ASTRO_FF_DIRECT_EXCHANGES = ASTRO_FF_BUY_EXCHANGES | ASTRO_FF_SELL_EXCHANGES
_admin_prefix_pattern = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_sync_lock = threading.Lock()
_pending_sync_lock = threading.Lock()
_route_dedupe_lock = threading.Lock()
_log_throttle_lock = threading.Lock()
_log_throttle: dict[tuple[str, str, str, str, str, str, str], float] = {}
_existing_route_snapshot: set[tuple[str, str, str, str]] = set()
_active_sync_routes: set[tuple[str, str, str, str]] = set()
_pending_submission_routes: set[tuple[str, str, str, str]] = set()
_existing_route_snapshot_at_monotonic = 0.0
_confirmation_stop = threading.Event()
_confirmation_thread: threading.Thread | None = None
_confirmation_lock = threading.Lock()

# A running card is expected to be protected from automatic cleanup.  Keep one
# audit record per concrete card/reason each day instead of emitting a warning
# on every cleanup pass.
_CLEANUP_BLOCKED_LOG_INTERVAL_SECONDS = 24 * 60 * 60


class AstroSdkError(RuntimeError):
    pass


class AstroSdkNotExecuted(AstroSdkError):
    """Documented pre-action refusal, distinct from an ambiguous server error."""
    pass


class AstroSdkRateDeferred(AstroSdkNotExecuted):
    """Quota pacing, or a documented 429 refusal; never an uncertain write."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def astro_verify_poll_seconds() -> float:
    return min(2.0, _env_float("ASTRO_AUTO_CARD_VERIFY_POLL_SECONDS", 0.4, 0.1))


def astro_sdk_read_total_seconds() -> float:
    return min(10.0, _env_float("ASTRO_SDK_READ_TOTAL_SECONDS", 3.0, 0.25))


def astro_revalidation_workers() -> int:
    return _env_int("ASTRO_FINAL_REVALIDATION_WORKERS", 3, 1, 4)


def astro_chain_label_publish_enabled() -> bool:
    return _env_bool("ASTRO_CHAIN_LABEL_PUBLISH_ENABLED", True)


def _astro_chain_label_ssh_command() -> list[str]:
    target = (
        os.environ.get("ASTRO_CHAIN_LABEL_SSH_TARGET")
        or os.environ.get("ASTRO_MANUAL_ORDER_SSH_TARGET")
        or os.environ.get("ASTRO_QUOTE_SSH_TARGET")
        or "ubuntu@192.0.2.10"
    ).strip()
    key = Path(
        os.environ.get("ASTRO_CHAIN_LABEL_SSH_KEY")
        or os.environ.get("ASTRO_MANUAL_ORDER_SSH_KEY")
        or os.environ.get("ASTRO_QUOTE_SSH_KEY")
        or "/home/example/Downloads/astro.pem"
    ).expanduser()
    container = (
        os.environ.get("ASTRO_CHAIN_LABEL_CONTAINER")
        or os.environ.get("ASTRO_MANUAL_ORDER_CONTAINER")
        or "astro-app"
    ).strip()
    updater_path = os.environ.get(
        "ASTRO_CHAIN_LABEL_UPDATER_PATH",
        "/home/ubuntu/astro-admin/dist/auto-chain-label-update.cjs",
    ).strip()
    if not target or not container or not updater_path:
        raise AstroSdkError("Astro 链标签发布配置不完整")
    if not key.is_file():
        raise AstroSdkError(f"Astro 链标签 SSH 密钥不存在：{key}")
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=4",
        target,
        "sudo",
        "-n",
        "docker",
        "exec",
        "-i",
        container,
        "node",
        updater_path,
    ]


def _publish_astro_chain_label(pair: dict[str, Any], astro_pair: dict[str, Any]) -> bool:
    card_id = str(astro_pair.get("id") or "").strip()
    label = str(pair.get("_chainNote") or "").strip()
    if str(pair.get("buyEx") or "").lower() == "pancakeswapv3":
        return False
    if not astro_chain_label_publish_enabled() or not card_id or not label:
        return False
    dex_config = pair.get("_dexConfig") if isinstance(pair.get("_dexConfig"), dict) else {}
    payload = {
        "id": card_id,
        "text": label,
        "symbol": str(pair.get("name") or "").strip().upper(),
        "chainIndex": str(dex_config.get("chainIndex") or "").strip(),
        "contractAddress": str(dex_config.get("contractAddress") or "").strip(),
        "updatedAt": int(time.time() * 1000),
    }
    try:
        completed = subprocess.run(
            _astro_chain_label_ssh_command(),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "远端更新失败").strip()
            raise AstroSdkError(error[-400:])
        acknowledgement = json.loads(completed.stdout)
        if acknowledgement.get("ok") is not True or str(acknowledgement.get("id")) != card_id or acknowledgement.get("text") != label:
            raise AstroSdkError("备注发布回读确认不匹配")
        _log(
            "astro_chain_label_published",
            message=f"Astro 卡片链标签已发布：{payload['symbol']} · {label}",
            details={"symbol": payload["symbol"], "cardId": card_id, "chainLabel": label},
        )
        return True
    except Exception as exc:
        _log(
            "astro_chain_label_publish_failed",
            level="warning",
            message=f"Astro 卡片已创建，但链标签发布失败：{payload['symbol']}",
            details={"symbol": payload["symbol"], "cardId": card_id, "chainLabel": label, "error": str(exc)},
        )

    return False


def _queue_astro_chain_label_publish(pair: dict[str, Any], astro_pair: dict[str, Any] | None) -> None:
    if not isinstance(astro_pair, dict) or not astro_chain_label_publish_enabled() or pair.get('buyEx') == 'pancakeswapv3':
        return
    def publish():
        from app.astro_transfer_labels import collect
        prepared = dict(pair)
        try:
            transfer_note, evidence = collect(pair)
            prepared['_chainNote'] = '；'.join(filter(None, [str(pair.get('_chainNote') or ''), transfer_note]))
            _log('astro_transfer_label_checked', message=f"Astro 充提备注已检查：{pair.get('name')}",
                 details={'symbol':pair.get('name'),'cardId':astro_pair.get('id'),
                          'transferNote':transfer_note,'checks':evidence,'blocksCreation':False})
        except Exception as exc:
            _log('astro_transfer_label_failed', level='warning', message='充提备注查询失败，卡片已正常创建',
                 details={'symbol':pair.get('name'),'errorType':type(exc).__name__})
        if prepared.get('_chainNote'):
            from app.astro_label_queue import enqueue
            enqueue(prepared, astro_pair)
    threading.Thread(
        target=publish,
        name=f"astro-chain-label-{astro_pair.get('id')}",
        daemon=True,
    ).start()


def _saved_auto_card_settings() -> dict[str, Any]:
    explicit = os.environ.get("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", "").strip()
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR", "").strip()
    path = Path(explicit).expanduser() if explicit else (
        Path(data_dir).expanduser() / "astro-spread-subscriptions.json" if data_dir else None
    )
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def astro_fs_borrow_auto_card_enabled() -> bool:
    saved = _saved_auto_card_settings().get("fsBorrowAutoCardEnabled")
    if isinstance(saved, bool):
        return saved
    return _env_bool("ASTRO_FS_BORROW_AUTO_CARD_ENABLED", True)


def astro_fs_borrow_min_cycle_profit_pct() -> float:
    saved = _finite_number(_saved_auto_card_settings().get("fsBorrowMinCycleProfitPct"))
    if saved is not None:
        return max(0.0, min(saved, 100.0))
    return min(100.0, _env_float("ASTRO_FS_BORROW_MIN_CYCLE_PROFIT_PCT", 0.2, 0.0))


def astro_fs_borrow_min_open_spread_pct() -> float:
    saved = _finite_number(_saved_auto_card_settings().get("fsBorrowMinOpenSpreadPct"))
    if saved is not None:
        return max(0.01, min(saved, 100.0))
    return min(100.0, _env_float("ASTRO_FS_BORROW_MIN_OPEN_SPREAD_PCT", 1.0, 0.01))


def astro_fs_borrow_min_volume_usdt() -> float:
    saved = _finite_number(_saved_auto_card_settings().get("minVolumeUsdt"))
    if saved is not None:
        return max(0.0, saved)
    return _env_float("ASTRO_SPREAD_MIN_VOLUME_USDT", 200_000.0, 0.0)


def _saved_alert_pct(key: str, env_name: str, default: float) -> float | None:
    saved = _saved_auto_card_settings()
    raw = saved.get(key) if key in saved else os.environ.get(env_name, str(default))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value) or value <= 0:
        return None
    return min(value, 100.0)


def astro_greater_price_alert_pct() -> float | None:
    return _saved_alert_pct("greaterPriceAlertPct", "ASTRO_AUTO_CARD_GREATER_PRICE_ALERT_PCT", 2.0)


def astro_greater_price_alert() -> str:
    """Return Astro's decimal-ratio value for the configured spread alert."""
    alert_pct = astro_greater_price_alert_pct()
    return _number_text(alert_pct / 100) if alert_pct is not None else ""


def astro_price_change_alert_pct() -> float | None:
    return _saved_alert_pct("priceChangeAlertPct", "ASTRO_AUTO_CARD_PRICE_CHANGE_ALERT_PCT", 0.0)


def astro_price_change_alert() -> str:
    """Return Astro's decimal-ratio value; an empty value disables this alert."""
    alert_pct = astro_price_change_alert_pct()
    return _number_text(alert_pct / 100) if alert_pct is not None else ""


def astro_min_notional_usdt() -> float:
    saved = _finite_number(_saved_auto_card_settings().get("minNotionalUsdt"))
    if saved is not None:
        return max(0.01, min(saved, 1_000_000.0))
    return _env_float("ASTRO_AUTO_CARD_MIN_NOTIONAL_USDT", 6.0, 0.01)


def astro_max_notional_usdt(config: AstroSdkConfig | None = None) -> float:
    fallback = config.max_trade_usdt if config is not None else _env_float("ASTRO_AUTO_CARD_MAX_TRADE_USDT", 40.0, 0.01)
    saved = _finite_number(_saved_auto_card_settings().get("maxNotionalUsdt"))
    value = fallback if saved is None else saved
    return max(astro_min_notional_usdt(), min(value, 1_000_000.0))


def astro_price_change_alert_only_rise() -> bool:
    saved = _saved_auto_card_settings().get("priceChangeAlertOnlyRise")
    if isinstance(saved, bool):
        return saved
    return _env_bool("ASTRO_AUTO_CARD_PRICE_CHANGE_ALERT_ONLY_RISE", False)


@dataclass(frozen=True)
class AstroSdkConfig:
    base_url: str
    admin_prefix: str
    api_key: str
    enabled: bool
    dry_run: bool
    tls_verify: bool
    timeout_seconds: float
    restart_wait_seconds: float
    max_cards_per_scan: int
    max_trade_usdt: float
    leverage: float
    spot_margin_type: str
    dex_api_path: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.admin_prefix and self.api_key)

    @property
    def api_path(self) -> str:
        return f"/{self.admin_prefix}/api/config/sdk-update-pair"

    @property
    def dex_configured(self) -> bool:
        return self.configured and bool(self.dex_api_path)


PairRevalidator = Callable[
    [dict[str, Any], AstroSdkConfig],
    tuple[dict[str, Any] | None, dict[str, Any]],
]
PairSubmitGuard = Callable[
    [dict[str, Any]],
    tuple[bool, dict[str, Any]],
]
CleanupRevalidator = Callable[
    [dict[str, Any], AstroSdkConfig, dict[str, Any]],
    tuple[bool, dict[str, Any]],
]


@dataclass
class _SyncBatch:
    pairs: list[dict[str, Any]]
    config: AstroSdkConfig
    revalidator: PairRevalidator | None
    submit_guard: PairSubmitGuard | None
    route_observations: dict[tuple[str, str, str, str], dict[str, Any]]
    cleanup_revalidator: CleanupRevalidator | None
    priority: bool = False


_pending_sync_batches: deque[_SyncBatch] = deque()


def astro_sdk_config() -> AstroSdkConfig:
    base_url = os.environ.get("ASTRO_SDK_BASE_URL", "").strip().rstrip("/")
    admin_prefix = os.environ.get("ASTRO_SDK_ADMIN_PREFIX", "").strip().strip("/")
    if admin_prefix and not _admin_prefix_pattern.fullmatch(admin_prefix):
        admin_prefix = ""
    margin_type = os.environ.get("ASTRO_AUTO_CARD_SPOT_MARGIN_TYPE", "cross").strip().lower()
    if margin_type not in {"spot", "cross", "isolated"}:
        margin_type = "cross"
    dex_api_path = os.environ.get("ASTRO_SDK_DEX_API_PATH", "").strip()
    if dex_api_path and not dex_api_path.startswith("/"):
        dex_api_path = "/" + dex_api_path
    return AstroSdkConfig(
        base_url=base_url,
        admin_prefix=admin_prefix,
        api_key=os.environ.get("ASTRO_SDK_API_KEY", "").strip(),
        enabled=_env_bool("ASTRO_AUTO_CARD_ENABLED", False),
        dry_run=_env_bool("ASTRO_AUTO_CARD_DRY_RUN", False),
        tls_verify=_env_bool("ASTRO_SDK_TLS_VERIFY", True),
        timeout_seconds=_env_float("ASTRO_SDK_TIMEOUT_SECONDS", 12.0, 2.0),
        restart_wait_seconds=_env_float("ASTRO_AUTO_CARD_RESTART_WAIT_SECONDS", 6.0, 3.0),
        # Zero means no local per-round creation cap. Cards are still
        # deduplicated, revalidated and submitted sequentially.
        max_cards_per_scan=_env_int("ASTRO_AUTO_CARD_MAX_PER_SCAN", 0, 0, 10000),
        max_trade_usdt=_env_float("ASTRO_AUTO_CARD_MAX_TRADE_USDT", 40.0, 0.01),
        leverage=_env_float("ASTRO_AUTO_CARD_LEVERAGE", 3.0, 1.0),
        spot_margin_type=margin_type,
        dex_api_path=dex_api_path,
    )


def astro_auto_card_status(config: AstroSdkConfig | None = None) -> dict[str, Any]:
    resolved = config or astro_sdk_config()
    pending_submissions = _refresh_pending_submission_routes()
    if not resolved.enabled:
        state = "disabled"
        message = "Astro 自动建卡未启用。"
    elif not resolved.configured:
        state = "not_configured"
        message = "Astro 地址已保存，等待配置 SDK API Key。"
    elif resolved.dry_run:
        state = "dry_run"
        message = "Astro 自动建卡处于演练模式，不会提交卡片。"
    elif _sync_lock.locked():
        state = "syncing"
        message = "Astro 正在同步交易卡片。"
    else:
        state = "ready"
        message = "Astro 自动建卡已就绪；新卡片默认暂停。"
    return {
        "enabled": resolved.enabled,
        "configured": resolved.configured,
        "dryRun": resolved.dry_run,
        "state": state,
        "message": message,
        "baseUrl": resolved.base_url or None,
        "adminPrefix": resolved.admin_prefix or None,
        "defaultPaused": True,
        "defaultDisableOpen": False,
        "maxCardsPerScan": resolved.max_cards_per_scan,
        "unlimitedCardsPerScan": resolved.max_cards_per_scan == 0,
        "defaultLeverage": resolved.leverage,
        "defaultMinNotionalUsdt": astro_min_notional_usdt(),
        "defaultMaxNotionalUsdt": astro_max_notional_usdt(resolved),
        "verificationMode": "fast_poll",
        "verificationPollSeconds": astro_verify_poll_seconds(),
        "verificationTimeoutSeconds": resolved.restart_wait_seconds,
        "sdkReadTotalSeconds": astro_sdk_read_total_seconds(),
        "sdkRequestBudget": budget_for(resolved.base_url).snapshot(),
        "sdkListReads": list_reads.snapshot(),
        "readIoMetrics": io_snapshot(),
        "labelDelivery": astro_label_queue.status(),
        "pendingSubmissionCount": pending_submissions["count"],
        "pendingSubmissions": pending_submissions,
        "defaultGreaterPriceAlertPct": astro_greater_price_alert_pct(),
        "defaultPriceChangeAlertPct": astro_price_change_alert_pct(),
        "defaultPriceChangeAlertOnlyRise": astro_price_change_alert_only_rise(),
        "dexConfiguration": {
            "enabled": resolved.dex_configured,
            "mode": "signed_sdk" if resolved.dex_configured else "local_manual_confirmation",
            "message": (
                "Astro DEX 配置 SDK 已接入；OKXDEX 建卡前会先回读并补充币配置。"
                if resolved.dex_configured
                else "Astro DEX 币由你手工配置；本地只按币名确认后允许建卡，链和合约地址不参与拦截。"
            ),
        },
        "automaticCleanup": astro_cleanup_status(),
    }


def canonical_body(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def sign_astro_request(api_key: str, timestamp: int, nonce: str, api_path: str, raw_body: str) -> str:
    canonical = "\n".join((str(timestamp), nonce, "POST", api_path, raw_body))
    return hmac.new(api_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


class AstroSdkClient:
    def __init__(self, config: AstroSdkConfig, transport: httpx.BaseTransport | None = None) -> None:
        if not config.configured:
            raise AstroSdkError("Astro SDK 配置不完整")
        self.config = config
        self.deadline_support = True
        self._deadline_loop: asyncio.AbstractEventLoop | None = None
        self._deadline_client: httpx.AsyncClient | None = None
        self._transport = transport
        self._budget = budget_for(config.base_url)
        self.client = httpx.Client(
            timeout=config.timeout_seconds,
            verify=config.tls_verify,
            transport=transport,
            # Astro is addressed by its public gateway directly.  Keeping the
            # card writer outside the exchange proxy path lets a verified
            # Pulse fallback card still be submitted during a proxy outage.
            trust_env=False,
            headers={"User-Agent": "stock-review-mac/astro-sdk"},
        )

    def close(self) -> None:
        try:
            if self._deadline_loop is not None and self._deadline_client is not None:
                self._deadline_loop.run_until_complete(asyncio.wait_for(self._deadline_client.aclose(), timeout=1.0))
        except Exception as exc:
            _log("astro_sdk_close_failed", level="warning", message="Astro SDK 连接清理未完成。", details={"error": str(exc)})
        finally:
            if self._deadline_loop is not None:
                self._deadline_loop.close()
            self.client.close()

    def __enter__(self) -> AstroSdkClient:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    async def _post_until(self, url: str, *, content: bytes, headers: dict[str, str], deadline: float) -> httpx.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Astro SDK request deadline exhausted")
        if self._deadline_client is None:
            self._deadline_client = httpx.AsyncClient(
                verify=self.config.tls_verify,
                transport=self._transport,
                trust_env=False,
                headers={"User-Agent": "stock-review-mac/astro-sdk"},
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Astro SDK request deadline exhausted")
        # wait_for bounds the complete network operation, including DNS,
        # connection, upload and body reads. Cancellation leaves no background
        # write thread; an add timeout is still an uncertain remote outcome.
        phase_started = {}
        async def trace(name, info):
            phase, _, event = name.rpartition(".")
            if event == "started":
                phase_started[phase] = time.monotonic()
                self._io_timing["lastPhase"] = phase
            elif event in {"complete", "failed"} and phase in phase_started:
                if event == "failed" and "failedPhase" not in self._io_timing:
                    self._io_timing["failedPhase"] = phase
                self._io_timing.setdefault("phasesMs", {})[phase] = round((time.monotonic() - phase_started.pop(phase)) * 1000, 1)
        return await asyncio.wait_for(
            self._deadline_client.post(url, content=content, headers=headers, timeout=min(self.config.timeout_seconds, remaining), extensions={"trace": trace}),
            timeout=remaining,
        )

    def request(self, payload: dict[str, Any], *, api_path: str | None = None, deadline: float | None = None) -> dict[str, Any]:
        started = time.monotonic()
        self._io_timing = {"success": False}
        try:
            result = self._request(payload, api_path=api_path, deadline=deadline)
            self._io_timing["success"] = True
            return result
        except Exception as exc:
            self._io_timing["errorType"] = type(exc).__name__
            raise
        finally:
            self._io_timing["durationMs"] = round((time.monotonic() - started) * 1000, 1)
            if payload.get("action") == "list":
                record_io("sdkList", self._io_timing)

    def _request(self, payload: dict[str, Any], *, api_path: str | None = None, deadline: float | None = None) -> dict[str, Any]:
        quota_started = time.monotonic()
        self._io_timing["lastPhase"] = "local_quota"
        try:
            self._budget.wait(deadline if deadline is not None else time.monotonic() + self.config.timeout_seconds,
                              consume=True, no_wait=payload.get("action") in {"add", "update", "delete"})
        except BudgetDeferred as exc:
            raise AstroSdkRateDeferred(str(exc)) from exc
        finally:
            self._io_timing["quotaWaitMs"] = round((time.monotonic() - quota_started) * 1000, 1)
        self._io_timing["lastPhase"] = "network"
        resolved_path = api_path or self.config.api_path
        raw_body = canonical_body(payload)
        timestamp = int(time.time() * 1000)
        nonce = secrets.token_urlsafe(24)[:32]
        signature = sign_astro_request(
            self.config.api_key,
            timestamp,
            nonce,
            resolved_path,
            raw_body,
        )
        headers = {
                "Content-Type": "application/json",
                "x-timestamp": str(timestamp),
                "x-nonce": nonce,
                "x-sign": signature,
        }
        if payload.get("action") == "add" and isinstance(getattr(self, "_submission_pair", None), dict):
            record_submission_request(self._submission_pair, nonce, timestamp)
        try:
            if deadline is None:
                response = self.client.post(self.config.base_url + resolved_path, content=raw_body.encode("utf-8"), headers=headers)
            else:
                if deadline <= time.monotonic():
                    raise TimeoutError("Astro SDK request deadline exhausted")
                if self._deadline_loop is None:
                    self._deadline_loop = asyncio.new_event_loop()
                response = self._deadline_loop.run_until_complete(self._post_until(self.config.base_url + resolved_path, content=raw_body.encode("utf-8"), headers=headers, deadline=deadline))
        finally:
            if payload.get("action") == "add" and (getattr(self, "_submission_pair", None) or {}).get("_announcementCard"):
                self._budget.listing_submitted()
        if response.status_code == 429:
            self._budget.limited(response.headers.get("Retry-After"))
            raise AstroSdkRateDeferred("The rate limit has been reached.")
        try:
            body = response.json()
        except ValueError as exc:
            raise AstroSdkError(f"Astro SDK 返回非 JSON：HTTP {response.status_code}") from exc
        if response.status_code >= 400 or not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("message") if isinstance(body, dict) else None
            if message == "The rate limit has been reached.":
                self._budget.limited(response.headers.get("Retry-After"))
                raise AstroSdkRateDeferred(str(message))
            if message in {"The rate limit has been reached.", "please run「set api-key xxx」in dev code",
                           "bad x-timestamp, please adjust your time!"}:
                raise AstroSdkNotExecuted(str(message))
            raise AstroSdkError(f"Astro SDK 请求失败：HTTP {response.status_code} · {message or '未知错误'}")
        return body

    def prepare_creation(self, *, listing=False):
        # Leave room for the final list/add/readback before obtaining fresh books.
        try:
            return self._budget.wait(time.monotonic() + 11.0, slots=3, listing=listing)
        except BudgetDeferred as exc:
            raise AstroSdkRateDeferred(str(exc)) from exc

    def list_pairs(self, *, deadline: float | None = None) -> list[dict[str, Any]]:
        body = self.request({"action": "list"}, deadline=deadline)
        data = body.get("data")
        if not isinstance(data, list):
            raise AstroSdkError("Astro SDK list 返回格式异常")
        return [item for item in data if isinstance(item, dict)]
    def add_pair(self, pair: dict[str, Any], *, deadline: float | None = None) -> None:
        public_pair = {key: value for key, value in pair.items() if not key.startswith("_")}
        self._submission_pair = pair
        try:
            self.request({"action": "add", "pair": public_pair}, deadline=deadline)
        finally:
            self._submission_pair = None

    def delete_pair(self, pair_id: str, *, deadline: float | None = None) -> None:
        if not str(pair_id or "").strip():
            raise AstroSdkError("Astro delete 缺少卡片 ID")
        self.request({"action": "delete", "pair": {"id": str(pair_id)}}, deadline=deadline)

    def list_dex_coins(self) -> list[dict[str, Any]]:
        if not self.config.dex_configured:
            raise AstroSdkError("Astro DEX 配置 SDK 未配置")
        body = self.request({"action": "list"}, api_path=self.config.dex_api_path)
        data = body.get("data")
        if not isinstance(data, list):
            raise AstroSdkError("Astro DEX 配置 list 返回格式异常")
        return [item for item in data if isinstance(item, dict)]

    def add_dex_coin(self, coin: dict[str, Any]) -> None:
        if not self.config.dex_configured:
            raise AstroSdkError("Astro DEX 配置 SDK 未配置")
        self.request({"action": "add", **coin}, api_path=self.config.dex_api_path)

    def ensure_dex_coin(self, coin: dict[str, Any]) -> dict[str, Any]:
        expected = {
            "name": str(coin.get("name") or "").strip().upper(),
            "chainIndex": str(coin.get("chainIndex") or "").strip(),
            "contractAddress": str(coin.get("contractAddress") or "").strip(),
            "quote": str(coin.get("quote") or "USDT").strip().upper(),
            "slippage": str(coin.get("slippage") or "1").strip(),
        }
        if not all((expected["name"], expected["chainIndex"], expected["contractAddress"])):
            raise AstroSdkError("OKXDEX 币配置缺少币名、链或合约地址")

        def normalized(item: dict[str, Any]) -> dict[str, str]:
            return {
                "name": str(item.get("name") or "").strip().upper(),
                "chainIndex": str(item.get("chainIndex") or "").strip(),
                "contractAddress": str(item.get("contractAddress") or "").strip(),
                "quote": str(item.get("quote") or "").strip().upper(),
                "slippage": str(item.get("slippage") or "").strip(),
            }

        existing = [item for item in self.list_dex_coins() if normalized(item)["name"] == expected["name"]]
        if existing:
            actual = normalized(existing[0])
            if actual != expected:
                raise AstroSdkError(
                    f"Astro 已有 {expected['name']} DEX 配置与 Pulse 元数据不一致，禁止自动覆盖"
                )
            return {"action": "existing", "coin": expected}

        self.add_dex_coin(expected)
        deadline = time.monotonic() + max(1.0, self.config.restart_wait_seconds)
        while time.monotonic() < deadline:
            for item in self.list_dex_coins():
                if normalized(item) == expected:
                    return {"action": "created", "coin": expected}
            time.sleep(min(astro_verify_poll_seconds(), max(0.0, deadline - time.monotonic())))
        raise AstroSdkError(f"Astro DEX 币配置添加后未能回读确认：{expected['name']}")


def active_astro_pairs_for_symbol(pairs: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """Return cards that may trade ``symbol``.

    Astro uses ``status=False`` for a paused card. Missing or unfamiliar
    status values are deliberately treated as active: a manual order guard
    must fail closed rather than assume an older/newer payload is harmless.
    """
    base = str(symbol or "").strip().upper()
    if base.endswith("USDT"):
        base = base[:-4]
    return [
        pair
        for pair in pairs
        if str(pair.get("name") or "").strip().upper() == base
        and pair.get("status") is not False
    ]


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _number_text(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def assess_astro_fs_borrow_signal(signal: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    symbol = str(signal.get("symbol") or "").strip().upper()
    futures_exchange = str(signal.get("futuresExchange") or "").strip().lower()
    checks = signal.get("checks") if isinstance(signal.get("checks"), dict) else {}
    bitget = checks.get("bg") if isinstance(checks.get("bg"), dict) else {}
    funding_rate = _finite_number(signal.get("currentFundingRate"))
    period_hours = _finite_number(signal.get("periodHours"))
    borrow_period_rate = _finite_number(bitget.get("borrowPeriodRate"))
    if borrow_period_rate is None and period_hours is not None and period_hours > 0:
        hourly_rate = _finite_number(bitget.get("hourlyBorrowRate"))
        daily_rate = _finite_number(bitget.get("dailyBorrowRate"))
        if hourly_rate is not None:
            borrow_period_rate = hourly_rate * period_hours
        elif daily_rate is not None:
            borrow_period_rate = daily_rate / 24 * period_hours
    open_spread_rate = _finite_number(bitget.get("openSpreadRate"))
    if open_spread_rate is None:
        open_spread_rate = _finite_number(signal.get("openSpreadRate"))
    volume_24h = _finite_number(signal.get("volume24h"))
    borrowable_amount = _finite_number(bitget.get("borrowableAmount"))
    borrowable_value_usdt = _finite_number(bitget.get("borrowableValueUsdt"))
    inventory_available = bitget.get("inventoryAvailable") is True or bitget.get("canBorrow") is True
    cycle_profit_rate = (
        -funding_rate - borrow_period_rate
        if funding_rate is not None and borrow_period_rate is not None
        else None
    )
    min_profit_pct = astro_fs_borrow_min_cycle_profit_pct()
    min_spread_pct = astro_fs_borrow_min_open_spread_pct()
    min_volume_usdt = astro_fs_borrow_min_volume_usdt()
    report = {
        "symbol": symbol,
        "futuresExchange": futures_exchange,
        "spotExchange": "bg",
        "fundingIntervalHours": period_hours,
        "currentFundingRate": funding_rate,
        "borrowHourlyRate": _finite_number(bitget.get("hourlyBorrowRate")),
        "borrowPeriodRate": borrow_period_rate,
        "cycleProfitRate": cycle_profit_rate,
        "cycleProfitPct": cycle_profit_rate * 100 if cycle_profit_rate is not None else None,
        "openSpreadRate": open_spread_rate,
        "openSpreadPct": open_spread_rate * 100 if open_spread_rate is not None else None,
        "volume24hUsdt": volume_24h,
        "borrowStatus": bitget.get("status"),
        "borrowableAmount": borrowable_amount,
        "borrowableValueUsdt": borrowable_value_usdt,
        "inventoryAvailable": inventory_available,
        "minCycleProfitPctExclusive": min_profit_pct,
        "minOpenSpreadPctExclusive": min_spread_pct,
        "minVolumeUsdt": min_volume_usdt,
    }
    reason = "eligible"
    if not astro_fs_borrow_auto_card_enabled():
        reason = "rule_disabled"
    elif not symbol or futures_exchange not in ASTRO_EXCHANGE_NAMES:
        reason = "unsupported_route"
    elif funding_rate is None or period_hours is None or period_hours <= 0:
        reason = "funding_or_period_missing"
    elif borrow_period_rate is None:
        reason = "bitget_borrow_rate_missing"
    elif cycle_profit_rate is None or cycle_profit_rate * 100 <= min_profit_pct:
        reason = "cycle_profit_below_threshold"
    elif open_spread_rate is None or open_spread_rate * 100 <= min_spread_pct:
        reason = "open_spread_below_threshold"
    elif volume_24h is None or volume_24h < min_volume_usdt:
        reason = "futures_volume_below_threshold"
    elif borrowable_amount is None:
        reason = "bitget_borrowable_amount_missing"
    elif borrowable_amount <= 0:
        reason = "bitget_borrowable_amount_zero"
    elif not inventory_available:
        reason = "bitget_borrow_inventory_not_executable"
    report["reason"] = reason
    return reason == "eligible", report


def build_astro_fs_pair(signal: dict[str, Any], config: AstroSdkConfig) -> dict[str, Any]:
    symbol = str(signal.get("symbol") or "").strip().upper()
    futures_exchange = ASTRO_EXCHANGE_NAMES.get(str(signal.get("futuresExchange") or "").lower())
    spot_exchange = ASTRO_EXCHANGE_NAMES.get(str(signal.get("spotExchange") or "").lower())
    opening_spread = _finite_number(signal.get("openSpreadRate"))
    if not symbol or not futures_exchange or not spot_exchange or opening_spread is None:
        raise AstroSdkError("机会缺少币种、交易所或可执行开仓差价")
    min_trade = astro_min_notional_usdt()
    max_trade = astro_max_notional_usdt(config)
    return {
        "name": symbol,
        "status": False,
        "type": "FS",
        # FS openSpreadRate already uses the same decimal-ratio unit as Astro
        # (0.01234 means 1.234%).  Multiplying by 100 creates a 123.4% rule.
        "openPosition": _number_text(opening_spread),
        "disableOpen": False,
        "closePosition": _number_text(_env_float("ASTRO_AUTO_CARD_CLOSE_POSITION", 0.0, 0.0)),
        "disableClose": False,
        "maxTradeUSDT": _number_text(max_trade),
        "leverage": "3",
        "buyEx": futures_exchange,
        "sellEx": spot_exchange,
        "startTime": "0",
        "minNotional": _number_text(min_trade),
        "maxNotional": _number_text(max_trade),
        # FS 借币卡只按建卡规则与盘口复核筛选，不写入 Astro 差价报警。
        "greaterPriceAlert": "",
        "priceAlert": astro_price_change_alert(),
        "priceAlertOnlyRise": astro_price_change_alert_only_rise(),
        "spotMarginType": "cross",
    }


def build_astro_fs_borrow_pairs(
    signals: list[dict[str, Any]],
    config: AstroSdkConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    for signal in signals:
        eligible, report = assess_astro_fs_borrow_signal(signal)
        reason = str(report.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
        if not eligible:
            continue
        normalized = {
            **signal,
            "spotExchange": "bg",
            "openSpreadRate": report["openSpreadRate"],
        }
        pair = build_astro_fs_pair(normalized, config)
        pair["_fsCandidate"] = {
            "symbol": report["symbol"],
            "exchange": report["futuresExchange"],
            "fundingRate": report["currentFundingRate"],
            "periodHours": report["fundingIntervalHours"],
            "fundingUpdatedAt": signal.get("fundingTime"),
            "premiumRate": signal.get("premiumRate"),
            "negativePotential": True,
            "volume24h": signal.get("volume24h"),
        }
        pair["_fsAssessment"] = report
        pairs.append(pair)
    return pairs, {
        "enabled": astro_fs_borrow_auto_card_enabled(),
        "evaluatedCount": len(signals),
        "eligibleCount": len(pairs),
        "reasons": reasons,
    }


def build_astro_spread_pair(candidate: dict[str, Any], config: AstroSdkConfig) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "").strip().upper()
    pair_type = str(candidate.get("type") or "").strip().upper()
    buy_exchange = str(candidate.get("buyExchange") or "").strip().lower()
    sell_exchange = str(candidate.get("sellExchange") or "").strip().lower()
    opening_spread_pct = _finite_number(candidate.get("openSpreadPct"))
    if pair_type not in {"SF", "FF"}:
        raise AstroSdkError("Pulse 差价建卡当前仅支持 SF/FF")
    if not symbol or not buy_exchange or not sell_exchange or opening_spread_pct is None:
        raise AstroSdkError("差价候选缺少币种、交易所或开仓差价")
    min_trade = astro_min_notional_usdt()
    max_trade = astro_max_notional_usdt(config)
    pair = {
        "name": symbol,
        "status": False,
        "type": pair_type,
        # Pulse candidates expose spreads in percentage points (for example,
        # 2.99 means 2.99%). Astro stores percentage fields as decimal ratios,
        # so the API value must be 0.0299 rather than 2.99.
        "openPosition": _number_text(opening_spread_pct / 100),
        "disableOpen": False,
        "closePosition": _number_text(_env_float("ASTRO_AUTO_CARD_CLOSE_POSITION", 0.0, -10.0)),
        "disableClose": False,
        "maxTradeUSDT": _number_text(max_trade),
        "leverage": _number_text(config.leverage),
        "buyEx": buy_exchange,
        "sellEx": sell_exchange,
        "startTime": "0",
        "minNotional": _number_text(min_trade),
        "maxNotional": _number_text(max_trade),
        "greaterPriceAlert": astro_greater_price_alert(),
        "priceAlert": astro_price_change_alert(),
        "priceAlertOnlyRise": astro_price_change_alert_only_rise(),
    }
    system_rearm_open_position = _finite_number(candidate.get("_systemDeleteRearmOpenPosition"))
    if system_rearm_open_position is not None:
        pair["_systemDeleteRearmOpenPosition"] = system_rearm_open_position
    if isinstance(candidate.get("_pipeline"), dict):
        pair["_pipeline"] = dict(candidate["_pipeline"])
    # Keep the scan-time liquidity evidence local to the worker.  Underscore
    # fields are stripped before the Astro API call, but let the JIT validator
    # fail closed if Pulse omitted either leg's 24h amount.
    pair["_buyVolume24hUsdt"] = candidate.get("buyVolume24hUsdt")
    pair["_sellVolume24hUsdt"] = candidate.get("sellVolume24hUsdt")
    if candidate.get("priorityNewListing") is True:
        pair["_priorityNewListing"] = True
    dex_config = candidate.get("dexConfig")
    if pair_type == "SF" and buy_exchange in {"okxdex", "pancakeswapv3"} and isinstance(dex_config, dict):
        pair["_dexConfig"] = dict(dex_config)
        if isinstance(candidate.get("okxdexExecutablePreflight"), dict):
            pair["_okxdexExecutablePreflight"] = dict(candidate["okxdexExecutablePreflight"])
        dex_mapping = candidate.get("dexMapping") if isinstance(candidate.get("dexMapping"), dict) else {}
        chain_index = str(dex_config.get("chainIndex") or "").strip()
        chain_label = str(dex_mapping.get("chainLabel") or dex_config.get("chainLabel") or chain_index).strip()
        if buy_exchange == "okxdex":
            pair["_chainNote"] = f"OKXDEX链：{chain_label or '未知链'}"
        pair["_chainLabel"] = chain_label or "未知链"
        pair["_dexMappingConfirmed"] = (
            bool(dex_mapping)
            and dex_mapping.get("status") == "confirmed"
        )
        if isinstance(candidate.get("targetSelection"), dict):
            pair["_targetSelection"] = dict(candidate["targetSelection"])
    return pair


def astro_ff_bybit_sell_exception_enabled() -> bool:
    return _saved_auto_card_settings().get("ffBybitSellExceptionEnabled") is True


def astro_ff_bybit_sell_exception(candidate: dict[str, Any]) -> bool:
    opening = _finite_number(candidate.get("openSpreadPct"))
    return (candidate.get("type") == "FF"
            and candidate.get("sellExchange") == "bybit"
            and candidate.get("buyExchange") in ASTRO_FF_BUY_EXCHANGES - {"bybit"}
            and opening is not None and opening > 10.0
            and astro_ff_bybit_sell_exception_enabled())


def astro_spread_card_routes(candidate: dict[str, Any]) -> list[tuple[str, str]]:
    pair_type = str(candidate.get("type") or "").strip().upper()
    buy_exchange = str(candidate.get("buyExchange") or "").strip().lower()
    sell_exchange = str(candidate.get("sellExchange") or "").strip().lower()
    if not buy_exchange or not sell_exchange:
        return []
    if pair_type == "SF":
        return [(buy_exchange, sell_exchange)]
    if pair_type != "FF":
        return []
    if astro_ff_bybit_sell_exception(candidate):
        return [(buy_exchange, sell_exchange)]
    if buy_exchange in ASTRO_FF_BUY_EXCHANGES and sell_exchange in ASTRO_FF_SELL_EXCHANGES:
        return [(buy_exchange, sell_exchange)]
    return []


def build_astro_spread_pairs(candidate: dict[str, Any], config: AstroSdkConfig) -> list[dict[str, Any]]:
    """Build direct Astro routes; automatic Gate CrossEx cards are disabled."""
    base_pair = build_astro_spread_pair(candidate, config)
    pairs: list[dict[str, Any]] = []
    for buy_exchange, sell_exchange in astro_spread_card_routes(candidate):
        pair = dict(base_pair)
        pair["buyEx"] = buy_exchange
        pair["sellEx"] = sell_exchange
        pairs.append(pair)
    return pairs


def _actionable_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        signal
        for signal in signals
        if signal.get("actionable") is True
        and not signal.get("watchOnly")
        and signal.get("inventoryAvailable") is True
        and signal.get("fundingWindowOk") is not False
        and _finite_number(signal.get("openSpreadRate")) is not None
    ]
    return sorted(eligible, key=lambda item: _finite_number(item.get("netFundingRate")) or float("-inf"), reverse=True)


def _log(event: str, *, level: str = "info", message: str, details: dict[str, Any] | None = None) -> None:
    safe_details = details or {}
    throttle_seconds = {
        "astro_card_skipped_revalidation": 60.0,
        "astro_card_existing_route_skipped": 300.0,
        "astro_card_cleanup_blocked": float(_CLEANUP_BLOCKED_LOG_INTERVAL_SECONDS),
        "astro_card_cleanup_revalidation_blocked": 60.0,
        "astro_card_rearm_suppressed": 300.0,
        "astro_card_cleanup_deferred_for_priority": 30.0,
    }.get(event)
    if throttle_seconds:
        key = (
            event,
            str(safe_details.get("symbol") or ""),
            str(safe_details.get("type") or ""),
            str(safe_details.get("buyEx") or ""),
            str(safe_details.get("sellEx") or ""),
            str(safe_details.get("reason") or ""),
            str(safe_details.get("cardId") or ""),
        )
        now = time.monotonic()
        with _log_throttle_lock:
            last_at = _log_throttle.get(key)
            if last_at is not None and now - last_at < throttle_seconds:
                return
            _log_throttle[key] = now
    append_system_runtime_event(
        event,
        level=level,
        source="backend",
        module="astro_sdk",
        message=message,
        error_type=safe_details.get("errorType"),
        duration_ms=safe_details.get("durationMs"),
        details=safe_details,
    )


def _sdk_list_pairs(client: AstroSdkClient, deadline: float) -> list[dict[str, Any]]:
    if deadline <= time.monotonic():
        raise TimeoutError("Astro SDK list total deadline exhausted")
    pairs = client.list_pairs(deadline=deadline) if getattr(client, "deadline_support", False) else client.list_pairs()
    if time.monotonic() > deadline:
        raise TimeoutError("Astro SDK list exceeded total deadline")
    return pairs


def _list_pairs_with_retry(client: AstroSdkClient, attempts: int = 4, *, deadline: float | None = None) -> list[dict[str, Any]]:
    deadline = time.monotonic() + astro_sdk_read_total_seconds() if deadline is None else deadline
    if not isinstance(client, AstroSdkClient) or not isinstance(getattr(client, "config", None), AstroSdkConfig):
        return _list_pairs_retry_read(client, attempts, deadline=deadline)
    # Separate deployments/accounts; never expose the key in diagnostics.
    key = (client.config.base_url, client.config.api_path, hashlib.sha256(client.config.api_key.encode()).hexdigest())
    try:
        return list_reads.read(key, lambda: _list_pairs_retry_read(client, attempts, deadline=deadline), deadline)
    except ReadDeferred as exc:
        raise AstroSdkRateDeferred(str(exc)) from exc


def _list_pairs_retry_read(client: AstroSdkClient, attempts: int = 4, *, deadline: float | None = None) -> list[dict[str, Any]]:
    started = time.monotonic()
    deadline = time.monotonic() + astro_sdk_read_total_seconds() if deadline is None else deadline
    last_error: Exception | None = None
    timings = []
    for attempt in range(attempts):
        attempt_started = time.monotonic()
        remaining = deadline - attempt_started
        # Do not abort a healthy TLS handshake just to reconnect within the
        # same three-second budget. Fast failures can still retry below.
        attempt_deadline = deadline
        try:
            result = _sdk_list_pairs(client, attempt_deadline)
            if attempt:
                _log("astro_sdk_list_recovered", message="Astro 卡片列表重试成功。",
                     details={"attempts": attempt + 1, "durationMs": round((time.monotonic() - started) * 1000, 1), "failedAttempts": timings})
            return result
        except AstroSdkRateDeferred:
            raise  # Respect the quota cooldown; do not retry four times.
        except Exception as exc:
            last_error = exc
            timings.append({"errorType": type(exc).__name__, "durationMs": round((time.monotonic() - attempt_started) * 1000, 1)})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if attempt + 1 < attempts:
                time.sleep(min(0.25 * (attempt + 1), remaining))
    raise AstroSdkError(f"Astro 卡片列表读取失败：{type(last_error).__name__}: {str(last_error).strip() or '无错误文本'}") from last_error


def _pair_identity(pair: dict[str, Any]) -> tuple[str, str, str, str]:
    return pair_identity(pair)


def _normalize_route_identity(route: Any) -> tuple[str, str, str, str] | None:
    if isinstance(route, dict):
        identity = _pair_identity(route)
    elif isinstance(route, (tuple, list)) and len(route) >= 4:
        identity = (
            str(route[0] or "").strip().upper(),
            str(route[1] or "").strip().upper(),
            str(route[2] or "").strip().lower(),
            str(route[3] or "").strip().lower(),
        )
    else:
        return None
    return identity if all(identity) else None


def _replace_existing_route_snapshot(pairs: list[dict[str, Any]]) -> None:
    global _existing_route_snapshot_at_monotonic
    if astro_chain_label_publish_enabled():
        try:
            astro_label_queue.schedule(pairs, _publish_astro_chain_label)
        except Exception as exc:
            _log("astro_label_queue_failed", level="warning", message="备注队列读取失败，建卡继续", details={"errorType": type(exc).__name__})
    routes = {_pair_identity(pair) for pair in pairs}
    for resolved in reconcile_pending_astro_submissions(pairs):
        _log("astro_card_submission_reconciled", message=f"Astro 待确认提交已从卡片列表补认：{resolved['route'][0]}", details=resolved)
    _refresh_pending_submission_routes()
    with _route_dedupe_lock:
        _existing_route_snapshot.clear()
        _existing_route_snapshot.update(route for route in routes if all(route))
        _existing_route_snapshot_at_monotonic = time.monotonic()


def _refresh_pending_submission_routes() -> dict[str, Any]:
    with _route_dedupe_lock:
        status = pending_astro_submission_status()
        _pending_submission_routes.clear()
        _pending_submission_routes.update(_pair_identity(item) for item in status["items"])
    return status


def _record_pending_submission(pair: dict[str, Any], state: str, error: str | None = None) -> None:
    with _route_dedupe_lock:
        record_pending_astro_submission(pair, state, error)
        _pending_submission_routes.add(_pair_identity(pair))


def _clear_pending_submission(pair: dict[str, Any]) -> None:
    with _route_dedupe_lock:
        clear_pending_astro_submission(pair)
        _pending_submission_routes.discard(_pair_identity(pair))


def _submission_confirmation_tick(config: AstroSdkConfig) -> None:
    due = pending_submission_checks_due()
    if not due:
        return
    try:
        with AstroSdkClient(config) as client:
            pairs = _list_pairs_with_retry(client, attempts=1, deadline=time.monotonic() + 6.0)
        _replace_existing_route_snapshot(pairs)
    except Exception as exc:
        record_submission_check(due, error=f"{type(exc).__name__}: {str(exc) or '卡片列表读取失败'}")
    else:
        record_submission_check(due)


def start_submission_confirmation_monitor() -> None:
    global _confirmation_thread
    with _confirmation_lock:
        if _confirmation_thread and _confirmation_thread.is_alive():
            return
        config = astro_sdk_config()
        if not config.configured:
            return
        _confirmation_stop.clear()
        def monitor():
            while not _confirmation_stop.is_set():
                try:
                    _submission_confirmation_tick(config)
                except Exception as exc:
                    _log("astro_submission_confirmation_failed", level="warning",
                         message="建卡结果核对失败，保留防重复锁",
                         details={"error": f"{type(exc).__name__}: {exc}"})
                _confirmation_stop.wait(1.0)
        _confirmation_thread = threading.Thread(target=monitor, name="astro-submission-confirmation", daemon=True)
        _confirmation_thread.start()


def stop_submission_confirmation_monitor() -> None:
    _confirmation_stop.set()
    if _confirmation_thread and _confirmation_thread.is_alive():
        _confirmation_thread.join(timeout=4.0)


def _mark_existing_route(route: tuple[str, str, str, str], *, present: bool) -> None:
    normalized = _normalize_route_identity(route)
    if normalized is None:
        return
    with _route_dedupe_lock:
        if present:
            _existing_route_snapshot.add(normalized)
        else:
            _existing_route_snapshot.discard(normalized)


def _set_active_sync_routes(pairs: list[dict[str, Any]]) -> None:
    routes = {_pair_identity(pair) for pair in pairs}
    with _route_dedupe_lock:
        _active_sync_routes.clear()
        _active_sync_routes.update(route for route in routes if all(route))


def astro_route_dedupe_state(route: Any) -> str | None:
    """Return an in-memory route blocker before any market-data API calls.

    The remote card list is refreshed by every SDK sync pass. Active and
    pending batches are also included so a hot route cannot be revalidated
    repeatedly while its first create attempt is still in flight.
    """

    normalized = _normalize_route_identity(route)
    if normalized is None:
        return None
    with _route_dedupe_lock:
        if normalized in _pending_submission_routes:
            return "submission_pending"
        if normalized in _existing_route_snapshot:
            return "existing"
        if normalized in _active_sync_routes:
            return "syncing"
    with _pending_sync_lock:
        if any(
            normalized == _pair_identity(pair)
            for batch in _pending_sync_batches
            for pair in batch.pairs
        ):
            return "queued"
    return None


def _wait_for_pair(
    client: AstroSdkClient,
    route: tuple[str, str, str, str],
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = time.monotonic()
    deadline = started + max(0.1, timeout_seconds)
    attempts = 0
    last_error: Exception | None = None
    while True:
        attempts += 1
        try:
            pairs = _sdk_list_pairs(client, deadline)
            if any(_pair_identity(item) == route for item in pairs):
                return pairs, {
                    "mode": "fast_poll",
                    "attempts": attempts,
                    "durationMs": round((time.monotonic() - started) * 1000, 1),
                }
        except Exception as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.05, poll_seconds), remaining))
    suffix = f"；最后错误：{last_error}" if last_error else ""
    raise AstroSdkError(
        f"Astro add 成功响应后 {timeout_seconds:.1f} 秒内未找到 "
        f"{route[0]} {route[2]}/{route[3]} 卡片{suffix}"
    )


def _wait_for_pair_absent(
    client: AstroSdkClient,
    route: tuple[str, str, str, str],
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + max(0.1, timeout_seconds)
    attempts = 0
    last_error: Exception | None = None
    while True:
        attempts += 1
        try:
            if not any(_pair_identity(item) == route for item in _sdk_list_pairs(client, deadline)):
                return {
                    "mode": "fast_poll",
                    "attempts": attempts,
                    "durationMs": round((time.monotonic() - started) * 1000, 1),
                }
        except Exception as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.05, poll_seconds), remaining))
    suffix = f"；最后错误：{last_error}" if last_error else ""
    raise AstroSdkError(
        f"Astro delete 成功响应后 {timeout_seconds:.1f} 秒内卡片仍存在："
        f"{route[0]} {route[2]}/{route[3]}{suffix}"
    )


_CLEANUP_ZERO_FIELDS = (
    "aExPosition",
    "bExPosition",
    "aMaxPos",
    "bMaxPos",
    "avgOpenAExPrice",
    "avgOpenBExPrice",
    "realizedProfit",
)
_CLEANUP_CONFIG_FIELDS = AUTO_CARD_CONFIG_FIELDS
_CLEANUP_REQUIRED_CONFIG_FIELDS = frozenset({
    "openPosition", "closePosition", "disableOpen", "disableClose",
    "maxTradeUSDT", "leverage", "minNotional", "maxNotional", "startTime",
    "greaterPriceAlert", "priceAlert", "priceAlertOnlyRise",
})


def _same_config_value(field: str, expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and actual is expected
    if isinstance(expected, (dict, list)) or isinstance(actual, (dict, list)):
        return json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True)
    if field in {"openPosition", "closePosition", "maxTradeUSDT", "leverage", "minNotional", "maxNotional", "startTime"}:
        expected_number = _finite_number(expected)
        actual_number = _finite_number(actual)
        return expected_number is not None and actual_number is not None and math.isclose(expected_number, actual_number, rel_tol=1e-9, abs_tol=1e-12)
    if expected is None or actual is None:
        return expected is None and actual is None
    expected_number = _finite_number(expected)
    actual_number = _finite_number(actual)
    if expected_number is None or actual_number is None:
        return type(expected) is type(actual) and expected == actual
    return math.isclose(expected_number, actual_number, rel_tol=1e-9, abs_tol=1e-12)


def _cleanup_safety_check(record: dict[str, Any], pair: dict[str, Any]) -> tuple[bool, str]:
    if pair.get("status") is not False:
        return False, "card_not_paused"
    if record.get("cleanupProtectedReason"):
        return False, str(record["cleanupProtectedReason"])
    pair_id = str(pair.get("id") or "").strip()
    registered_id = str(record.get("astroPairId") or "").strip()
    if not pair_id or (registered_id and pair_id != registered_id):
        return False, "card_id_missing_or_changed"
    snapshot = record.get("createdPair")
    if not isinstance(snapshot, dict) or not snapshot:
        return False, "legacy_record_without_creation_snapshot"
    if record.get("createdPairSnapshotVersion") != AUTO_CARD_SNAPSHOT_VERSION:
        return False, "legacy_record_without_complete_config_snapshot"
    submitted_only = record.get("submittedOnlyConfigFields")
    inactive_unreadable = inactive_unreadable_config_fields(snapshot, pair)
    unreadable = [str(field) for field in submitted_only if field not in inactive_unreadable and field not in pair] if isinstance(submitted_only, list) else []
    if unreadable:
        return False, f"submitted_config_not_readable:{','.join(unreadable)}"
    if pair_identity(snapshot) != pair_identity(pair):
        return False, "card_route_changed"
    for field in _CLEANUP_CONFIG_FIELDS:
        if field in inactive_unreadable:
            continue
        if field not in snapshot and field not in pair and field not in _CLEANUP_REQUIRED_CONFIG_FIELDS:
            continue
        if field not in snapshot or field not in pair or not _same_config_value(field, snapshot[field], pair[field]):
            return False, f"card_config_changed:{field}"
    for field in _CLEANUP_ZERO_FIELDS:
        value = _finite_number(pair.get(field))
        if value is None:
            return False, f"safety_field_missing:{field}"
        if not math.isclose(value, 0.0, rel_tol=0.0, abs_tol=1e-12):
            return False, f"card_has_trade_or_exposure:{field}"
    return True, "safe_never_traded_paused_card"


def _run_pair_revalidation(
    index: int,
    pair: dict[str, Any],
    config: AstroSdkConfig,
    revalidator: PairRevalidator,
) -> dict[str, Any]:
    started_at_ms = int(time.time() * 1000)
    try:
        revalidated_pair, report = revalidator(pair, config)
        error = None
    except Exception as exc:
        revalidated_pair = None
        report = None
        error = str(exc)
    completed_at_ms = int(time.time() * 1000)
    return {
        "index": index,
        "originalPair": pair,
        "pair": revalidated_pair,
        "report": report,
        "error": error,
        "startedAtMs": started_at_ms,
        "completedAtMs": completed_at_ms,
    }


def _core_not_ready_error(exc: Exception) -> bool:
    if not isinstance(exc, AstroSdkError):
        return False
    message = str(exc).lower().replace(" ", "")
    return "core" in message and ("notready" in message or "未准备好" in message or "还未准备好" in message)


def _add_pair_with_core_retry(
    client: AstroSdkClient,
    pair: dict[str, Any],
    attempts: int = 3,
    *,
    before_attempt: Callable[[int], bool] | None = None,
    deadline: float | None = None,
) -> int:
    """Retry only Astro's short restart window; never replay other failures."""

    last_error: Exception | None = None
    deadline = time.monotonic() + astro_sdk_read_total_seconds() if deadline is None else deadline
    for attempt in range(max(1, attempts)):
        if time.monotonic() >= deadline:
            raise TimeoutError("Astro SDK add total deadline exhausted")
        if before_attempt is not None and not before_attempt(attempt + 1):
            return 0
        try:
            if getattr(client, "deadline_support", False):
                client.add_pair(pair, deadline=deadline)
            else:
                client.add_pair(pair)
            return attempt + 1
        except Exception as exc:
            last_error = exc
            if _core_not_ready_error(exc):
                mark_submission_not_executed(pair, "Astro 明确返回 core not ready，未执行创建")
                _clear_pending_submission(pair)
            if not _core_not_ready_error(exc) or attempt + 1 >= attempts:
                raise
            time.sleep(min(0.5 * (attempt + 1), max(0.0, deadline - time.monotonic())))
    raise AstroSdkError(str(last_error or "Astro 建卡失败"))


def _submission_quote_freshness(
    report: dict[str, Any] | None,
    *,
    completed_at_ms: int,
    elapsed_seconds: float,
) -> tuple[bool, dict[str, Any]]:
    """Age the final independent quote proof right before each SDK add.

    Retain the validator's per-leg limits (including DEX), and use monotonic
    elapsed time as well as native timestamps so clock rollback cannot revive
    an expired quote. Earlier confirmation rounds need not remain current.
    """
    proof = report if isinstance(report, dict) else {}
    limits = proof.get("quoteAgeLimitsSeconds")
    limits = limits if isinstance(limits, dict) else {}
    now_ms = int(time.time() * 1000)
    details: dict[str, Any] = {"checkedAtMs": now_ms, "elapsedSinceRevalidationMs": round(max(0.0, elapsed_seconds) * 1000, 1), "legs": {}}
    for side in ("buy", "sell"):
        quote = proof.get(f"{side}Quote")
        quote = quote if isinstance(quote, dict) else {}
        timestamp = _finite_number(quote.get("timestamp"))
        limit = _finite_number(limits.get(side))
        if timestamp is None or timestamp <= 0 or limit is None or limit <= 0:
            return False, {**details, "reason": "submit_quote_evidence_missing", "side": side}
        age_at_completion = max(0.0, (completed_at_ms - timestamp) / 1000)
        reported_age = _finite_number(quote.get("quoteAgeSeconds"))
        age = max(0.0, (now_ms - timestamp) / 1000, max(age_at_completion, reported_age or 0.0) + max(0.0, elapsed_seconds))
        details["legs"][side] = {"timestamp": timestamp, "ageSeconds": round(age, 3), "maxAgeSeconds": limit}
        if age > limit:
            return False, {**details, "reason": "submit_quote_expired", "side": side}
    return True, {**details, "reason": "eligible"}


def _process_cleanup_item(
    client: AstroSdkClient,
    item: dict[str, Any],
    config: AstroSdkConfig,
    cleanup_revalidator: CleanupRevalidator | None,
) -> tuple[str, str, str, str] | None:
    card = item["pair"]
    record = item["record"]
    observation = item["observation"]
    route = _pair_identity(card)
    safe, safety_reason = _cleanup_safety_check(record, card)
    if not safe:
        reset_auto_card_invalid_observation(route)
        _log(
            "astro_card_cleanup_blocked",
            level="info" if safety_reason == "card_not_paused" else "warning",
            message=f"Astro 自动清理已保护卡片，不执行删除：{route[0]}",
            details={
                "symbol": route[0],
                "type": route[1],
                "buyEx": route[2],
                "sellEx": route[3],
                "cardId": str(card.get("id") or ""),
                "reason": safety_reason,
            },
        )
        return None
    cleanup_report: dict[str, Any] = {}
    if _creation_sync_pending():
        _log("astro_card_cleanup_deferred_for_priority", message="自动清理已让位给待建卡路线。", details={"symbol": route[0]})
        return None
    if cleanup_revalidator is not None:
        try:
            delete_confirmed, cleanup_report = cleanup_revalidator(card, config, observation)
        except Exception as exc:
            delete_confirmed = False
            cleanup_report = {"reason": "cleanup_revalidation_error", "error": str(exc)}
        if not delete_confirmed:
            reset_auto_card_invalid_observation(route)
            _log(
                "astro_card_cleanup_revalidation_blocked",
                message=f"Astro 自动清理最终复核未通过，保留卡片：{route[0]}",
                details={"symbol": route[0], "type": route[1], "buyEx": route[2], "sellEx": route[3], **cleanup_report},
            )
            return None
    delete_reason = str(observation.get("reason") or "rule_no_longer_eligible")
    details = {
        "symbol": route[0],
        "type": route[1],
        "buyEx": route[2],
        "sellEx": route[3],
        "invalidForSeconds": item["invalidForSeconds"],
        "reason": delete_reason,
        "safety": safety_reason,
        "finalRevalidation": cleanup_report,
    }
    if config.dry_run:
        _log("astro_card_cleanup_dry_run", message=f"Astro 演练自动清理：{route[0]}（未实际删除）", details=details)
        return None
    # A pending candidate takes precedence over optional card housekeeping.
    if _creation_sync_pending():
        _log("astro_card_cleanup_deferred_for_priority", message="自动清理已让位给待建卡路线。", details=details)
        return None
    # Market revalidation can take seconds. Never delete from its old SDK
    # snapshot: the user may have resumed, edited or traded this card meanwhile.
    # The remote API has no conditional-delete token, so a residual race after
    # this read remains and must not be advertised as an atomic protection.
    try:
        matches = [item for item in _sdk_list_pairs(client, time.monotonic() + astro_sdk_read_total_seconds()) if str(item.get("id") or "") == str(card["id"])]
        fresh_card = matches[0] if len(matches) == 1 else None
        fresh_safe, fresh_reason = (
            _cleanup_safety_check(record, fresh_card)
            if fresh_card is not None else (False, "card_id_missing_or_ambiguous")
        )
    except Exception as exc:
        fresh_safe, fresh_reason = False, "cleanup_card_refresh_failed"
        details["error"] = str(exc)
    if not fresh_safe:
        reset_auto_card_invalid_observation(route)
        _log("astro_card_cleanup_blocked", message=f"Astro 删除前状态复核已保护卡片：{route[0]}", details={**details, "reason": fresh_reason, "cardId": str(card["id"])})
        return None
    if _creation_sync_pending():
        _log("astro_card_cleanup_deferred_for_priority", message="自动清理已让位给待建卡路线。", details=details)
        return None
    if getattr(client, "deadline_support", False):
        client.delete_pair(str(card["id"]), deadline=time.monotonic() + astro_sdk_read_total_seconds())
    else:
        client.delete_pair(str(card["id"]))
    details["verification"] = _wait_for_pair_absent(
        client,
        route,
        timeout_seconds=config.restart_wait_seconds,
        poll_seconds=astro_verify_poll_seconds(),
    )
    mark_auto_card_system_deleted(route, delete_reason)
    _log("astro_card_auto_deleted", message=f"Astro 已自动清理失效且从未成交的暂停卡片：{route[0]}", details=details)
    return route


def _prepare_dex_route(
    client: AstroSdkClient,
    pair: dict[str, Any],
    config: AstroSdkConfig,
) -> tuple[bool, dict[str, Any]]:
    dex_config = pair.get("_dexConfig")
    if not isinstance(dex_config, dict):
        return True, {}
    local_note = str(pair.get("_chainNote") or "").strip() or None
    details: dict[str, Any] = {
        # Astro's SDK has no card-note field.  The post-create label bridge
        # publishes this metadata to Astro's browser-side label store.
        "localChainNote": local_note,
        "chainNoteVisibility": "astro_label_bridge",
    }
    manual_mapping_confirmed = pair.get("_dexMappingConfirmed") is True
    if (not config.dex_configured or pair.get("buyEx") == "pancakeswapv3") and not manual_mapping_confirmed:
        return False, {**details, "reason": "dex_mapping_unconfirmed"}
    if manual_mapping_confirmed:
        details["dexConfiguration"] = {
            "action": "manual_confirmed",
            "chainIndex": dex_config.get("chainIndex"),
            "contractAddress": dex_config.get("contractAddress"),
        }
        return True, details
    details["dexConfiguration"] = client.ensure_dex_coin(dex_config)
    return True, details


def _sync_candidate_pair(
    client: AstroSdkClient,
    original_pair: dict[str, Any],
    config: AstroSdkConfig,
    revalidator: PairRevalidator | None,
    existing_routes: set[tuple[str, str, str, str]],
    submit_guard: PairSubmitGuard | None = None,
) -> bool:
    symbol = str(original_pair.get("name") or "").strip().upper()
    route = _pair_identity(original_pair)
    initial_open_position = original_pair.get("openPosition")
    pipeline = dict(original_pair.get("_pipeline") or {})
    route_started_ms = int(time.time() * 1000)
    pipeline["routeStartedAtMs"] = route_started_ms
    enqueued_at = _finite_number(pipeline.get("queuedAtMs"))
    if enqueued_at is not None:
        pipeline["routeQueueWaitMs"] = max(0, round(route_started_ms - enqueued_at, 1))

    def guard_allows(candidate: dict[str, Any], stage: str) -> bool:
        if submit_guard is None:
            return True
        try:
            allowed, guard_report = submit_guard(candidate)
        except Exception as exc:
            allowed = False
            guard_report = {"reason": "submit_guard_error", "error": str(exc)}
        if allowed:
            return True
        _log(
            "astro_card_submit_guard_blocked",
            message=f"Astro 建卡被最新屏蔽规则拦截：{symbol}",
            details={
                "symbol": symbol,
                "type": route[1],
                "buyEx": route[2],
                "sellEx": route[3],
                "stage": stage,
                **(guard_report if isinstance(guard_report, dict) else {}),
            },
        )
        return False

    # The candidate may have been queued before the user saved a new block.
    # Re-read the caller's live policy before doing any remote work.
    if not guard_allows(original_pair, "before_remote_checks"):
        return False

    # Optional annotation prefetch runs beside revalidation, outside its latency budget.
    if astro_chain_label_publish_enabled() and not config.dry_run:
        try:
            from app.astro_transfer_labels import prefetch
            prefetch(original_pair)
        except Exception:
            pass  # Transfer status is a remark, never a creation/opening gate.

    dex_ready, dex_details = _prepare_dex_route(client, original_pair, config)
    if not dex_ready:
        _log(
            "astro_dex_config_blocked",
            level="warning",
            message=f"Astro DEX 链和合约未精确确认，已跳过卡片：{symbol}",
            details={"symbol": symbol, "type": route[1], "buyEx": route[2], "sellEx": route[3], **dex_details},
        )
        return False

    # The worker already loaded the batch snapshot. Keep the authoritative
    # remote dedupe immediately before add, without another pre-JIT list read.
    if route in existing_routes:
        return False
    _refresh_pending_submission_routes()
    if route in _pending_submission_routes:
        _log("astro_card_submission_pending", message=f"Astro 上次提交结果待确认，不重复建卡：{symbol}", details={"symbol": symbol, "route": route})
        return False

    prepare = getattr(client, "prepare_creation", None)
    if callable(prepare) and not config.dry_run:
        pipeline["sdkPacingWaitMs"] = round(prepare(listing=isinstance(original_pair.get("_announcementCard"), dict)), 1)

    started_at_ms = int(time.time() * 1000)
    if revalidator is None:
        pair, revalidation, error = original_pair, None, ""
    else:
        result = _run_pair_revalidation(0, original_pair, config, revalidator)
        pair = result.get("pair")
        revalidation = result.get("report") if isinstance(result.get("report"), dict) else None
        error = str(result.get("error") or "").strip()
        started_at_ms = int(result.get("startedAtMs") or started_at_ms)
    completed_at_ms = int(time.time() * 1000)
    completed_at_monotonic = time.monotonic()
    pipeline.update({
        "jitRevalidationStartedAtMs": started_at_ms,
        "jitRevalidationCompletedAtMs": completed_at_ms,
        "jitRevalidationDurationMs": max(0, completed_at_ms - started_at_ms),
        "jitRevalidationMode": "per_card_immediately_before_submit",
    })
    if error:
        _log(
            "astro_card_revalidation_failed",
            level="warning",
            message=f"Astro 建卡最终复核失败，已跳过：{symbol}",
            details={"symbol": symbol, "type": route[1], "buyEx": route[2], "sellEx": route[3], "error": error, "pipeline": pipeline},
        )
        return False
    if not isinstance(pair, dict):
        _log(
            "astro_card_skipped_revalidation",
            message=f"Astro 建卡最终复核未通过，已跳过：{symbol}",
            details={"symbol": symbol, "type": route[1], "buyEx": route[2], "sellEx": route[3], "initialOpenPosition": initial_open_position, "pipeline": pipeline, **(revalidation or {})},
        )
        return False
    if _pair_identity(pair) != route or pair_lifecycle_identity(pair) != pair_lifecycle_identity(original_pair):
        _log(
            "astro_card_revalidation_failed",
            level="warning",
            message=f"Astro 建卡最终复核路线或 DEX 指纹不一致，已跳过：{symbol}",
            details={
                "symbol": symbol,
                "expectedRoute": pair_lifecycle_identity(original_pair),
                "actualRoute": pair_lifecycle_identity(pair),
                "pipeline": pipeline,
            },
        )
        return False
    pair["_pipeline"] = pipeline
    safe_details: dict[str, Any] = {
        "symbol": symbol,
        "type": pair["type"],
        "buyEx": pair["buyEx"],
        "sellEx": pair["sellEx"],
        "openPosition": pair["openPosition"],
        "initialOpenPosition": initial_open_position,
        "disableOpen": pair["disableOpen"],
        "revalidation": revalidation,
        "pipeline": pipeline,
        **dex_details,
    }
    if isinstance(pair.get("_targetSelection"), dict):
        safe_details["targetSelection"] = pair["_targetSelection"]
    if config.dry_run:
        _log("astro_card_dry_run", message=f"Astro 演练建卡：{symbol}", details=safe_details)
        return True

    # Recheck the route directly before add.  Astro cannot encode a DEX chain
    # in the pair key, so any existing same-route card also blocks a different
    # local DEX fingerprint and is logged instead of duplicated.
    submit_deadline = time.monotonic() + astro_sdk_read_total_seconds()
    if revalidator is not None and str(pair.get("type") or "").upper() in {"SF", "FF"}:
        fresh, freshness = _submission_quote_freshness(revalidation, completed_at_ms=completed_at_ms, elapsed_seconds=time.monotonic() - completed_at_monotonic)
        if not fresh:
            _log("astro_card_submit_quote_expired", message=f"Astro 提交前行情证据失效，等待重新复核：{symbol}", details={**safe_details, "reason": freshness["reason"], "submitFreshness": freshness, "submitAttempt": 0})
            return False
        lifetime = min(leg["maxAgeSeconds"] - leg["ageSeconds"] for leg in freshness["legs"].values())
        submit_deadline = min(submit_deadline, time.monotonic() + max(0.0, lifetime))
    try:
        submit_existing = _sdk_list_pairs(client, submit_deadline)
    except (TimeoutError, httpx.TimeoutException) as exc:
        _log("astro_card_submit_quote_expired", message=f"Astro 提交前读取超过行情有效期，等待重新复核：{symbol}", details={**safe_details, "reason": "submit_deadline_exhausted", "error": str(exc), "submitAttempt": 0})
        return False
    if any(_pair_identity(item) == route for item in submit_existing):
        existing_routes.add(route)
        _log(
            "astro_card_submit_deduplicated",
            message=f"Astro 提交前已存在同路线卡片，已跳过：{symbol}",
            details={"symbol": symbol, "route": route, "lifecycleRoute": pair_lifecycle_identity(pair)},
        )
        return False
    # This is intentionally the last operation before add_pair.  It closes the
    # race where a route was blocked while JIT depth checks or Astro list_pairs
    # were still in flight.
    def before_add_attempt(attempt: int) -> bool:
        if pair.get("type") == "FF" and pair.get("sellEx") == "bybit":
            opening = _finite_number(pair.get("openPosition"))
            if not astro_ff_bybit_sell_exception({"type": "FF", "buyExchange": pair.get("buyEx"),
                    "sellExchange": "bybit", "openSpreadPct": opening * 100 if opening is not None else None}):
                _log("astro_card_bybit_exception_blocked", message=f"Bybit 卖出腿例外不再满足，等待重新复核：{symbol}")
                return False
        if not guard_allows(pair, "immediately_before_add" if attempt == 1 else "before_add_retry"):
            return False
        # JIT may lower the spread after the candidate passed the rearm gate.
        # Preserve the system-cleanup threshold even if a revalidator rebuilt
        # the dict; never mutate deletion/pullback state during this check.
        rearm_pair = {**original_pair, **pair}
        rearm_allowed, rearm_report = check_delete_rearm_before_submit(rearm_pair)
        safe_details["rearmCheck"] = rearm_report
        if not rearm_allowed:
            _log("astro_card_rearm_suppressed", message=f"Astro 提交前已不满足重建条件：{symbol}",
                 details={**safe_details, **rearm_report, "stage": "immediately_before_add"})
            return False
        if revalidator is not None and str(pair.get("type") or "").upper() in {"SF", "FF"}:
            fresh, freshness = _submission_quote_freshness(
                revalidation,
                completed_at_ms=completed_at_ms,
                elapsed_seconds=time.monotonic() - completed_at_monotonic,
            )
            safe_details["submitFreshness"] = freshness
            if not fresh:
                _log("astro_card_submit_quote_expired", message=f"Astro 提交前行情证据失效，等待重新复核：{symbol}", details={**safe_details, "reason": freshness["reason"], "submitAttempt": attempt})
                return False
        _record_pending_submission(pair, "submitting")
        return True

    submit_started_at_ms = int(time.time() * 1000)
    try:
        add_attempts = _add_pair_with_core_retry(client, pair, before_attempt=before_add_attempt, deadline=submit_deadline)
    except Exception as exc:
        # A timed-out request may already have reached Astro. Keep a durable
        # route blocker instead of treating a missing acknowledgement as a
        # failed write and replaying it on the next scan.
        if isinstance(exc, (AstroSdkNotExecuted, httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            mark_submission_not_executed(pair, f"明确未执行：{type(exc).__name__}: {exc}")
            _clear_pending_submission(pair)
            _log("astro_card_submission_not_executed", level="warning", message=f"Astro 请求未执行，结束本次提交：{symbol}",
                 details={"symbol": symbol, "route": route, "error": f"{type(exc).__name__}: {exc}"})
        elif route in _pending_submission_routes:
            _record_pending_submission(pair, "outcome_unknown", f"{type(exc).__name__}: {str(exc) or '未收到明确提交结果'}")
        elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            _log("astro_card_submit_quote_expired", message=f"Astro 提交预算耗尽，等待重新复核：{symbol}", details={**safe_details, "reason": "submit_deadline_exhausted", "error": str(exc)})
            return False
        raise
    if not add_attempts:
        _clear_pending_submission(pair)
        return False
    _record_pending_submission(pair, "awaiting_confirmation")
    submitted_at_ms = int(time.time() * 1000)
    pipeline.update({
        "astroSubmitStartedAtMs": submit_started_at_ms,
        "astroSubmittedAtMs": submitted_at_ms,
        "astroSubmitDurationMs": max(0, submitted_at_ms - submit_started_at_ms),
        "astroSubmitAttempts": add_attempts,
    })
    safe_details["pipeline"] = pipeline
    _log("astro_card_submitted", message=f"Astro 已提交新卡片：{symbol}（已暂停）", details=safe_details)
    try:
        verified_pairs, verification = _wait_for_pair(
            client,
            route,
            timeout_seconds=config.restart_wait_seconds,
            poll_seconds=astro_verify_poll_seconds(),
        )
    except Exception as exc:
        _record_pending_submission(pair, "awaiting_confirmation", str(exc))
        _log("astro_card_submission_unconfirmed", level="warning", message=f"Astro 已提交但回读超时，保留待确认状态：{symbol}", details={**safe_details, "error": str(exc)})
        return False
    created_at_ms = int(time.time() * 1000)
    pipeline.update({"createdAtMs": created_at_ms, "astroVerificationDurationMs": verification.get("durationMs")})
    first_seen_at_ms = _finite_number(pipeline.get("firstSeenAtMs"))
    confirmed_at_ms = _finite_number(pipeline.get("confirmedAtMs"))
    if first_seen_at_ms is not None:
        pipeline["totalFromFirstSeenMs"] = max(0, round(created_at_ms - first_seen_at_ms, 1))
    if confirmed_at_ms is not None:
        pipeline["postConfirmationMs"] = max(0, round(created_at_ms - confirmed_at_ms, 1))
    safe_details["verification"] = verification
    safe_details["pipeline"] = pipeline
    _log("astro_card_created", message=f"Astro 新卡片已确认：{symbol}（已暂停）", details=safe_details)
    verified_pair = next((item for item in verified_pairs if _pair_identity(item) == route), None)
    register_auto_created_pair(pair, astro_pair=verified_pair)
    _clear_pending_submission(pair)
    _queue_astro_chain_label_publish(pair, verified_pair)
    existing_routes.add(route)
    _mark_existing_route(route, present=True)
    return True


def _sync_pair_worker(
    pairs: list[dict[str, Any]],
    config: AstroSdkConfig,
    revalidator: PairRevalidator | None = None,
    route_observations: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
    cleanup_revalidator: CleanupRevalidator | None = None,
    priority: bool = False,
    submit_guard: PairSubmitGuard | None = None,
) -> None:
    started = time.monotonic()
    stage = "initialize"
    candidate_count = len(pairs)
    try:
        worker_started_at_ms = int(time.time() * 1000)
        for pair in pairs:
            pipeline = dict(pair.get("_pipeline") or {})
            enqueued_at_ms = _finite_number(pipeline.get("queuedAtMs"))
            pipeline["queuedAtMs"] = enqueued_at_ms if enqueued_at_ms is not None else worker_started_at_ms
            pipeline["workerStartedAtMs"] = worker_started_at_ms
            pipeline["queueWaitMs"] = max(0, worker_started_at_ms - pipeline["queuedAtMs"])
            pair["_pipeline"] = pipeline
        with AstroSdkClient(config) as client:
            stage = "list_pairs"
            existing = _list_pairs_with_retry(client)
            stage = "reconcile_routes"
            _replace_existing_route_snapshot(existing)

            cleanup_ready = observe_auto_card_cleanup(route_observations or {}, existing)
            pairs, suppressed, newly_guarded = apply_delete_rearm_rules(
                pairs,
                existing,
                route_observations=route_observations,
            )
            for item in suppressed:
                pair = item.get("pair") if isinstance(item.get("pair"), dict) else {}
                route = _pair_identity(pair)
                _log(
                    "astro_card_rearm_suppressed",
                    message=(
                        f"Astro 路线暂未重建：{route[0]} {route[2]}/{route[3]} · "
                        f"{item.get('reason') or 'unknown'}"
                    ),
                    details={
                        "symbol": route[0],
                        "type": route[1],
                        "buyEx": route[2],
                        "sellEx": route[3],
                        **{key: value for key, value in item.items() if key != "pair"},
                    },
                )
            for item in newly_guarded:
                pair = item["pair"]
                _log(
                    "astro_card_delete_rearm_started",
                    message=(
                        f"两次回读未发现 Astro 卡片，等待回落确认或直接突破后补建：{pair['name']} "
                        f"{pair['buyEx']}/{pair['sellEx']}"
                    ),
                    details={
                        "symbol": pair["name"],
                        "type": pair["type"],
                        "buyEx": pair["buyEx"],
                        "sellEx": pair["sellEx"],
                        "deletionReferenceOpenPosition": item["deletionReferenceOpenPosition"],
                        "rearmOpenPosition": item["rearmOpenPosition"],
                    },
                )
            existing_routes = {_pair_identity(item) for item in existing}
            queued_routes: set[tuple[str, ...]] = set()
            candidate_pairs: list[dict[str, Any]] = []
            queued_at_ms = int(time.time() * 1000)
            for pair in pairs:
                symbol = str(pair.get("name") or "").strip().upper()
                route = _pair_identity(pair)
                lifecycle_route = pair_lifecycle_identity(pair)
                if not symbol:
                    continue
                if route in existing_routes:
                    _log(
                        "astro_card_existing_route_skipped",
                        message=f"Astro 已存在同路线卡片，不重复建立：{symbol}",
                        details={
                            "symbol": symbol,
                            "type": route[1],
                            "buyEx": route[2],
                            "sellEx": route[3],
                            "reason": "existing_astro_route",
                        },
                    )
                    continue
                if lifecycle_route in queued_routes:
                    continue
                queued_routes.add(lifecycle_route)
                pipeline = dict(pair.get("_pipeline") or {})
                pipeline["batchPreparedAtMs"] = queued_at_ms
                pipeline["queuePosition"] = len(candidate_pairs) + 1
                pair["_pipeline"] = pipeline
                candidate_pairs.append(pair)

            created = 0
            for index, original_pair in enumerate(candidate_pairs):
                stage = "create_candidates"
                if not priority and _priority_sync_pending():
                    remaining = candidate_pairs[index:]
                    if remaining:
                        _enqueue_pending_sync(
                            _SyncBatch(
                                pairs=remaining,
                                config=config,
                                revalidator=revalidator,
                                submit_guard=submit_guard,
                                route_observations={},
                                cleanup_revalidator=None,
                                priority=False,
                            )
                        )
                    _log(
                        "astro_card_sync_preempted",
                        message="后台建卡批次已让位给 Astro 热点扫描建卡。",
                        details={"remainingCount": len(remaining)},
                    )
                    return
                if config.max_cards_per_scan > 0 and created >= config.max_cards_per_scan:
                    break
                route = _pair_identity(original_pair)
                if route in existing_routes:
                    continue
                try:
                    if _sync_candidate_pair(
                        client,
                        original_pair,
                        config,
                        revalidator,
                        existing_routes,
                        submit_guard,
                    ):
                        created += 1
                except AstroSdkRateDeferred:
                    _log("astro_card_rate_deferred", message=f"Astro 请求额度不足，路线留待下一轮：{route[0]}",
                         details={"symbol": route[0], "type": route[1], "buyEx": route[2], "sellEx": route[3], "reason": "sdk_rate_limit"})
                except Exception as exc:
                    _log(
                        "astro_card_sync_item_failed",
                        level="error",
                        message=f"Astro 单卡建立失败，已继续处理其他路线：{route[0]}",
                        details={"symbol": route[0], "type": route[1], "buyEx": route[2], "sellEx": route[3], "error": str(exc)},
                    )

            # Opportunity creation is latency-sensitive, while cleanup is
            # deliberately based on a sustained invalid observation.  Run the
            # expensive two-read cleanup checks only after all newly-triggered
            # routes have had their immediate submit chance.
            if _creation_sync_pending():
                if cleanup_ready:
                    _log(
                        "astro_card_cleanup_deferred_for_priority",
                        message="自动清理已让位给 Astro 热点扫描建卡，下一轮再复核。",
                        details={"cleanupCount": len(cleanup_ready[:2])},
                    )
                return
            for item in cleanup_ready[:2]:
                stage = "cleanup"
                if _creation_sync_pending():
                    break
                try:
                    deleted_route = _process_cleanup_item(client, item, config, cleanup_revalidator)
                    if deleted_route is not None:
                        existing = [existing_pair for existing_pair in existing if _pair_identity(existing_pair) != deleted_route]
                        _mark_existing_route(deleted_route, present=False)
                except Exception as exc:
                    card = item.get("pair") if isinstance(item.get("pair"), dict) else {}
                    route = _pair_identity(card)
                    _log(
                        "astro_card_cleanup_item_failed",
                        level="error",
                        message=f"Astro 单卡清理失败，已继续处理其他卡片：{route[0]}",
                        details={"symbol": route[0], "type": route[1], "buyEx": route[2], "sellEx": route[3], "error": str(exc)},
                    )
    except AstroSdkRateDeferred:
        _log("astro_sdk_read_deferred", message="Astro SDK 正在等待请求额度，下一轮继续读取。", details={"stage": stage})
    except Exception as exc:
        cause = exc
        while cause.__cause__ is not None and not isinstance(cause, (TimeoutError, httpx.TimeoutException)):
            cause = cause.__cause__
        # Exception messages can contain signed URLs. Log only a bounded,
        # redacted message and the concrete root exception type.
        error = re.sub(r'https?://\S+', '[redacted-url]', str(exc).strip())[:400]
        _log("astro_card_sync_failed", level="error",
             message=f"Astro 同步失败（{stage} / {type(cause).__name__}）：{error or '无错误文本'}",
             details={"candidateCount": candidate_count, "stage": stage,
                      "errorType": type(cause).__name__, "wrapperErrorType": type(exc).__name__,
                      "durationMs": round((time.monotonic() - started) * 1000, 1),
                      "priority": priority})
    finally:
        _set_active_sync_routes([])
        if _sync_lock.locked():
            _sync_lock.release()
        _start_next_pending_sync()


def _merge_sync_batches(existing: _SyncBatch, incoming: _SyncBatch) -> _SyncBatch:
    merged_pairs: dict[tuple[str, ...], dict[str, Any]] = {
        pair_lifecycle_identity(pair): pair for pair in existing.pairs
    }
    for pair in incoming.pairs:
        key = pair_lifecycle_identity(pair)
        previous = merged_pairs.get(key)
        previous_pipeline = previous.get("_pipeline", {}) if isinstance(previous, dict) else {}
        pipeline = dict(pair.get("_pipeline") or {})
        previous_queued_at = _finite_number(previous_pipeline.get("queuedAtMs"))
        if previous_queued_at is not None:
            pipeline["queuedAtMs"] = previous_queued_at
        merged_pairs[key] = {**pair, "_pipeline": pipeline}
    return _SyncBatch(
        pairs=list(merged_pairs.values()),
        config=incoming.config,
        revalidator=incoming.revalidator,
        submit_guard=incoming.submit_guard,
        route_observations={**existing.route_observations, **incoming.route_observations},
        cleanup_revalidator=incoming.cleanup_revalidator,
        priority=existing.priority or incoming.priority,
    )


def _enqueue_pending_sync(batch: _SyncBatch) -> int:
    with _pending_sync_lock:
        for index, pending in enumerate(_pending_sync_batches):
            if (
                pending.revalidator is batch.revalidator
                and pending.submit_guard is batch.submit_guard
                and pending.cleanup_revalidator is batch.cleanup_revalidator
            ):
                merged = _merge_sync_batches(pending, batch)
                if merged.priority and index > 0:
                    del _pending_sync_batches[index]
                    _pending_sync_batches.appendleft(merged)
                else:
                    _pending_sync_batches[index] = merged
                return len(_pending_sync_batches)
        if batch.priority:
            _pending_sync_batches.appendleft(batch)
        else:
            _pending_sync_batches.append(batch)
        return len(_pending_sync_batches)


def _priority_sync_pending() -> bool:
    with _pending_sync_lock:
        return any(batch.priority for batch in _pending_sync_batches)


def _creation_sync_pending() -> bool:
    with _pending_sync_lock:
        return any(batch.pairs for batch in _pending_sync_batches)


def _start_sync_batch(batch: _SyncBatch) -> None:
    _set_active_sync_routes(batch.pairs)
    thread = threading.Thread(
        target=_sync_pair_worker,
        args=(
            batch.pairs,
            batch.config,
            batch.revalidator,
            batch.route_observations,
            batch.cleanup_revalidator,
            batch.priority,
            batch.submit_guard,
        ),
        name="astro-auto-card-sync",
        daemon=True,
    )
    thread.start()


def _start_next_pending_sync() -> None:
    with _pending_sync_lock:
        if not _pending_sync_batches or not _sync_lock.acquire(blocking=False):
            return
        batch = _pending_sync_batches.popleft()
    _start_sync_batch(batch)


def _reset_sync_queue_for_tests() -> None:
    """Clear only the in-memory coalescing queue; never touches Astro state."""

    with _pending_sync_lock:
        _pending_sync_batches.clear()
    _set_active_sync_routes([])
    _replace_existing_route_snapshot([])


def schedule_astro_pairs(
    pairs: list[dict[str, Any]],
    config: AstroSdkConfig | None = None,
    *,
    revalidator: PairRevalidator | None = None,
    submit_guard: PairSubmitGuard | None = None,
    route_observations: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
    cleanup_revalidator: CleanupRevalidator | None = None,
    priority: bool = False,
) -> dict[str, Any]:
    resolved = config or astro_sdk_config()
    status = astro_auto_card_status(resolved)
    if not resolved.enabled or not resolved.configured:
        return status
    enqueued_at_ms = int(time.time() * 1000)
    queued_pairs = []
    for pair in pairs:
        pipeline = dict(pair.get("_pipeline") or {})
        pipeline.update(queuedAtMs=enqueued_at_ms, latestEnqueuedAtMs=enqueued_at_ms)
        queued_pairs.append({**pair, "_pipeline": pipeline})
    batch = _SyncBatch(
        pairs=queued_pairs,
        config=resolved,
        revalidator=revalidator,
        submit_guard=submit_guard,
        route_observations=dict(route_observations or {}),
        cleanup_revalidator=cleanup_revalidator,
        priority=priority,
    )
    if not _sync_lock.acquire(blocking=False):
        pending_count = _enqueue_pending_sync(batch)
        return {
            **status,
            "state": "queued_pending",
            "pendingBatchCount": pending_count,
            "message": (
                "上一轮 Astro 建卡仍在执行；热点路线已插入优先队列，不会丢弃。"
                if priority
                else "上一轮 Astro 建卡仍在执行；本轮已合并入待处理队列，不会丢弃。"
            ),
        }
    _start_sync_batch(batch)
    creation_limit = (
        "本轮新增卡片数量不设上限。"
        if resolved.max_cards_per_scan == 0
        else f"本轮最多新增 {resolved.max_cards_per_scan} 张卡。"
    )
    return {
        **status,
        "state": "queued",
        "pendingBatchCount": 0,
        "message": (
            f"已排队检查 {len(pairs)} 条建卡路线，并核对本地自动卡生命周期；"
            f"{creation_limit}"
        ),
    }


def schedule_astro_cards(signals: list[dict[str, Any]], config: AstroSdkConfig | None = None) -> dict[str, Any]:
    resolved = config or astro_sdk_config()
    candidates = _actionable_signals(signals)
    pairs = [build_astro_fs_pair(candidate, resolved) for candidate in candidates]
    return schedule_astro_pairs(pairs, resolved)

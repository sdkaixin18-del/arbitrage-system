"""Dedicated SSH tunnel and HTTP pool for read-only cloud depth verification."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

import httpx

_lock = threading.Lock()
_tunnel: subprocess.Popen | None = None
_retry_at = 0.0
_health_at = 0.0
_status: dict[str, Any] = {"state": "standby", "lastError": None}
_health_stop = threading.Event()
_health_thread: threading.Thread | None = None
_health_timeout_seconds = 2.0


def enabled() -> bool:
    return os.environ.get("ASTRO_DEPTH_CLOUD_ENABLED", "0").lower() in {"1", "true", "on"}


def origin() -> str:
    return "http://127.0.0.1:18767"


def _start_tunnel() -> None:
    global _tunnel, _retry_at
    with _lock:
        if _tunnel is not None and _tunnel.poll() is None:
            return
        if time.monotonic() < _retry_at:
            raise RuntimeError("腾讯云深度通道正在重连冷却")
        _retry_at = time.monotonic() + 5
        key = os.environ.get("ASTRO_DEPTH_CLOUD_SSH_KEY", "/home/example/Downloads/astro.pem")
        target = os.environ.get("ASTRO_DEPTH_CLOUD_SSH_TARGET", "ubuntu@192.0.2.10")
        if not Path(key).is_file() or target.startswith("-"):
            raise RuntimeError("腾讯云深度SSH配置不可用")
        _tunnel = subprocess.Popen([
            "/usr/bin/ssh", "-N", "-T", "-i", key, "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=3",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2", "-L", "127.0.0.1:18767:127.0.0.1:8767", target,
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _status.update(state="connecting", lastError=None)


def maintain(*, force: bool = False) -> dict[str, Any]:
    global _health_at
    if not enabled():
        return {"state": "disabled"}
    if not force and time.monotonic() - _health_at < 15:
        return status()
    previous_state = status().get("state")
    _health_at = time.monotonic()
    started = time.monotonic()
    stage = "tunnel"
    server_time_delta_ms = None
    try:
        _start_tunnel()
        stage = "health"
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(_health_timeout_seconds, connect=1.0)) as client:
            request_started = time.monotonic()
            request_started_ms = time.time() * 1000
            response = client.get(origin() + "/health")
            health_rtt_ms = round((time.monotonic() - request_started) * 1000, 1)
            request_finished_ms = time.time() * 1000
            response.raise_for_status()
            body = response.json()
        stage = "identity"
        if body.get("service") != "astro-depth-cloud" or body.get("protocol") != 1 or body.get("readOnly") is not True:
            raise RuntimeError("腾讯云深度服务身份不匹配")
        stage = "clock"
        server_time_delta_ms = round((request_started_ms + request_finished_ms) / 2 - float(body["serverTimeMs"]), 1)
        if abs(server_time_delta_ms) > 1000:
            raise RuntimeError("腾讯云与本机时钟偏差超过1秒")
        with _lock:
            _status.update(state="ready", lastError=None, lastProbeError=None,
                           lastProbeErrorType=None, lastProbeStage="complete",
                           lastProbeAtMs=int(time.time() * 1000),
                           lastProbeDurationMs=round((time.monotonic() - started) * 1000, 1),
                           lastHealthRttMs=health_rtt_ms, serverTimeDeltaMs=server_time_delta_ms,
                           consecutiveTimeouts=0, lastHealthyAtMs=int(time.time() * 1000),
                           healthCheckCount=int(_status.get("healthCheckCount") or 0) + 1)
    except Exception as exc:
        error = str(exc)[:300] or type(exc).__name__
        is_timeout = isinstance(exc, httpx.TimeoutException)
        with _lock:
            consecutive = int(_status.get("consecutiveTimeouts") or 0) + 1 if is_timeout else 0
            unavailable = not is_timeout or previous_state != "ready" or consecutive >= 2
            _status.update(state="unavailable" if unavailable else "ready",
                           lastError=error if unavailable else None,
                           lastProbeError=error, lastProbeErrorType=type(exc).__name__,
                           lastProbeStage=stage, lastProbeAtMs=int(time.time() * 1000),
                           lastProbeDurationMs=round((time.monotonic() - started) * 1000, 1),
                           serverTimeDeltaMs=server_time_delta_ms,
                           consecutiveTimeouts=consecutive,
                           healthCheckCount=int(_status.get("healthCheckCount") or 0) + 1,
                           healthCheckFailures=int(_status.get("healthCheckFailures") or 0) + 1)
        # Healthy SSH can outlive a failed remote service. Probe health again;
        # do not restart other tunnels or the trading gateway.
    current = status()
    if current.get("state") != previous_state:
        try:
            from app.system_runtime_log import append_system_runtime_event
            ready = current.get("state") == "ready"
            append_system_runtime_event(
                "astro_depth_cloud_ready" if ready else "astro_depth_cloud_unavailable",
                level="info" if ready else "warning", module="astro_depth_transport",
                message="腾讯云只读备用通道就绪" if ready else "腾讯云备用深度健康检查失败",
                details={"previousState": previous_state, **current},
            )
        except Exception:
            pass  # Diagnostic I/O must not terminate recovery monitoring.
    return current


def status() -> dict[str, Any]:
    with _lock:
        return {**_status, "enabled": enabled(), "independentConnection": True,
                "healthIntervalSeconds": 15, "probeTimeoutSeconds": _health_timeout_seconds,
                "readonly": True}


def start() -> None:
    global _health_thread
    if not enabled() or (_health_thread and _health_thread.is_alive()):
        return
    _health_stop.clear()
    def loop():
        while not _health_stop.is_set():
            current = maintain(force=True)
            _health_stop.wait(15 if current.get("state") == "ready" and not current.get("lastProbeError") else 2)
    _health_thread = threading.Thread(target=loop, name="astro-cloud-depth-health", daemon=True)
    _health_thread.start()


def stop() -> None:
    global _tunnel, _retry_at
    _health_stop.set()
    if _health_thread and _health_thread.is_alive():
        _health_thread.join(timeout=2)
    with _lock:
        process, _tunnel = _tunnel, None
        _retry_at = 0.0
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


class CloudPublicClient:
    cloud_depth = True

    def __init__(self, **_kwargs):
        if not enabled():
            raise RuntimeError("腾讯云深度备用未启用")
        _start_tunnel()
        self.client = httpx.Client(base_url=origin(), trust_env=False, timeout=httpx.Timeout(4.5, connect=1))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.client.close()

    def get(self, url: str, *, params=None):
        from app.crypto import api_request_timeout_kwargs, api_request_remaining_seconds
        response = self.client.post("/v1/public-get", json={"url": url, "params": params or {}},
                                    **api_request_timeout_kwargs(self.client))
        api_request_remaining_seconds()
        response.raise_for_status()
        envelope = response.json()
        if envelope.get("service") != "astro-depth-cloud" or envelope.get("url") != url or envelope.get("protocol") != 1:
            raise RuntimeError("腾讯云深度响应不匹配")
        received = int(envelope["receivedAtMs"])
        if received > time.time() * 1000 + 1000 or time.time() * 1000 - received > 3000:
            raise RuntimeError("腾讯云深度响应已陈旧或时钟偏差")
        headers = {"x-depth-received-at": str(received), "x-depth-request-started-at": str(envelope["requestStartedAtMs"])}
        if envelope.get("retryAfter"):
            headers["retry-after"] = str(envelope["retryAfter"])
        return httpx.Response(int(envelope["statusCode"]), json=envelope["payload"], headers=headers, request=httpx.Request("GET", url, params=params))

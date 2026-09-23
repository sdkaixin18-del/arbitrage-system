from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FundingCloudError(RuntimeError):
    message: str
    status_code: int = 503

    def __str__(self) -> str:
        return self.message


def funding_cloud_enabled() -> bool:
    return os.environ.get("FUNDING_CLOUD_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def funding_cloud_config() -> dict[str, Any]:
    target = os.environ.get(
        "FUNDING_CLOUD_SSH_TARGET",
        os.environ.get("ASTRO_MANUAL_ORDER_SSH_TARGET", "ubuntu@192.0.2.10"),
    ).strip()
    key = Path(
        os.environ.get(
            "FUNDING_CLOUD_SSH_KEY",
            os.environ.get("ASTRO_MANUAL_ORDER_SSH_KEY", "/home/example/Downloads/astro.pem"),
        )
    ).expanduser()
    rpc_path = os.environ.get(
        "FUNDING_CLOUD_RPC_PATH",
        "/opt/astro-funding-cloud/current/scripts/funding_cloud_rpc.py",
    ).strip()
    try:
        timeout_seconds = max(10.0, min(float(os.environ.get("FUNDING_CLOUD_TIMEOUT_SECONDS", "90")), 180.0))
    except ValueError:
        timeout_seconds = 90.0
    return {
        "target": target,
        "key": key,
        "rpcPath": rpc_path,
        "timeoutSeconds": timeout_seconds,
    }


def _ssh_command(config: dict[str, Any], *, fresh: bool = False) -> list[str]:
    key = Path(config["key"])
    if not key.is_file():
        raise FundingCloudError(f"腾讯云资金费 SSH 密钥不存在：{key}")
    command = [
        "/usr/bin/ssh",
        "-T",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=6",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if fresh:
        command.extend(["-o", "ControlMaster=no", "-o", "ControlPath=none"])
    else:
        socket_path = Path(tempfile.gettempdir()) / f"sr-funding-{os.getuid()}-{os.getpid()}-%C"
        command.extend(
            [
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=600",
                "-o",
                f"ControlPath={socket_path}",
            ]
        )
    command.extend([str(config["target"]), "python3", str(config["rpcPath"])])
    return command


def _invoke(envelope: dict[str, Any], *, fresh: bool = False) -> dict[str, Any]:
    config = funding_cloud_config()
    try:
        completed = subprocess.run(
            _ssh_command(config, fresh=fresh),
            input=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=float(config["timeoutSeconds"]),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FundingCloudError(f"腾讯云资金费服务连接失败：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"SSH exit {completed.returncode}"
        raise FundingCloudError(f"腾讯云资金费服务连接失败：{detail[:500]}")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FundingCloudError("腾讯云资金费服务返回了无效数据") from exc
    if not isinstance(response, dict):
        raise FundingCloudError("腾讯云资金费服务返回格式无效")
    return response


def funding_cloud_request(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not funding_cloud_enabled():
        raise FundingCloudError("腾讯云资金费服务尚未启用")
    envelope = {
        "method": method.upper(),
        "path": path,
        "query": query or {},
        "payload": payload,
    }
    try:
        response = _invoke(envelope)
    except FundingCloudError:
        # All funding-cloud calls are read-only or idempotent watch-list syncs.
        # One fresh transport retry avoids a stale multiplexed SSH socket.
        response = _invoke(envelope, fresh=True)
    status_code = int(response.get("statusCode") or 500)
    data = response.get("data")
    if not 200 <= status_code < 300:
        detail = data.get("detail") if isinstance(data, dict) else data
        raise FundingCloudError(str(detail or "腾讯云资金费服务请求失败"), status_code=status_code)
    if not isinstance(data, dict):
        raise FundingCloudError("腾讯云资金费服务未返回对象数据")
    data.setdefault("computeLocation", "tencent_cloud")
    return data


def funding_cloud_status() -> dict[str, Any]:
    if not funding_cloud_enabled():
        return {
            "enabled": False,
            "computeLocation": "local_paused",
            "status": "disabled",
        }
    try:
        payload = funding_cloud_request("GET", "/health")
        payload["enabled"] = True
        return payload
    except FundingCloudError as exc:
        return {
            "enabled": True,
            "computeLocation": "tencent_cloud",
            "status": "error",
            "error": str(exc),
        }

from __future__ import annotations

import os
from urllib.parse import quote

import httpx


DEFAULT_BARK_ICON_URL = "https://assets.example.invalid/assets/arb-system-push-v1.png"
DEFAULT_BARK_SOUND = "minuet"


def bark_status() -> str:
    return "ok" if os.environ.get("BARK_WEBHOOK_URL") or os.environ.get("BARK_URL") else "not_configured"


def bark_webhook_url() -> str | None:
    value = os.environ.get("BARK_WEBHOOK_URL") or os.environ.get("BARK_URL")
    return value.rstrip("/") if value else None


def send_bark_or_log(
    enabled: bool,
    title: str,
    body: str,
    group: str,
    url: str | None,
    disabled_message: str,
    icon_url: str | None = None,
    fallback_icon_url: str | None = DEFAULT_BARK_ICON_URL,
    trust_env: bool = True,
) -> tuple[str, str | None]:
    if not enabled:
        return "manual_only", disabled_message
    status = bark_status()
    message = None
    if status == "ok":
        try:
            webhook = bark_webhook_url()
            assert webhook
            params = {
                "group": group,
                "sound": os.environ.get("BARK_SOUND", DEFAULT_BARK_SOUND),
            }
            configured_icon = os.environ.get("BARK_ICON_URL", "").strip()
            if configured_icon == "https://assets.imedao.com/images/favicon.png":
                configured_icon = ""
            resolved_icon = icon_url or (
                configured_icon or fallback_icon_url
                if fallback_icon_url == DEFAULT_BARK_ICON_URL else fallback_icon_url
            )
            if resolved_icon:
                params["icon"] = resolved_icon
            if url:
                params["url"] = url
            with httpx.Client(timeout=8, trust_env=trust_env) as client:
                client.get(
                    f"{webhook}/{quote(title, safe='')}/{quote(body, safe='')}",
                    params=params,
                ).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            status = "error"
            message = str(exc)
    else:
        message = "Bark 未配置，事件已入库但未推送。"
    return status, message

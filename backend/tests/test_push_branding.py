from __future__ import annotations

import httpx
import pytest

from app import notifications


@pytest.mark.parametrize(
    "configured,kwargs,expected",
    [
        (None, {}, notifications.DEFAULT_BARK_ICON_URL),
        ("https://assets.imedao.com/images/favicon.png", {}, notifications.DEFAULT_BARK_ICON_URL),
        ("https://example.test/custom.png", {}, "https://example.test/custom.png"),
        ("https://example.test/global.png", {"icon_url": "https://example.test/module.png"}, "https://example.test/module.png"),
        ("https://example.test/global.png", {"fallback_icon_url": None}, None),
        ("https://example.test/global.png", {"fallback_icon_url": "https://example.test/fallback.png"}, "https://example.test/fallback.png"),
    ],
)
def test_shared_push_icon(monkeypatch, configured, kwargs, expected):
    monkeypatch.setenv("BARK_WEBHOOK_URL", "https://example.test/fake-device")
    monkeypatch.delenv("BARK_ICON_URL", raising=False)
    if configured is not None:
        monkeypatch.setenv("BARK_ICON_URL", configured)
    requests = []

    class Client:
        def __init__(self, **options):
            assert options["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, *, params):
            requests.append(params)
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(notifications.httpx, "Client", Client)
    assert notifications.send_bark_or_log(
        True, "Astro故障", "测试", "Astro扫描故障", None, "未启用",
        trust_env=False, **kwargs,
    ) == ("ok", None)
    assert len(requests) == 1
    assert requests[0].get("icon") == expected


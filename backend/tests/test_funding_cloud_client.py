from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest

from app import funding_cloud_client as client


def test_cloud_request_returns_cloud_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    key = tmp_path / "astro.pem"
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("FUNDING_CLOUD_ENABLED", "1")
    monkeypatch.setenv("FUNDING_CLOUD_SSH_KEY", str(key))

    def fake_run(*_args, **kwargs):
        envelope = json.loads(kwargs["input"])
        assert envelope["path"] == "/health"
        return CompletedProcess([], 0, stdout='{"statusCode":200,"data":{"status":"ok"}}', stderr="")

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    result = client.funding_cloud_request("GET", "/health")

    assert result["status"] == "ok"
    assert result["computeLocation"] == "tencent_cloud"


def test_cloud_request_preserves_remote_validation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    key = tmp_path / "astro.pem"
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("FUNDING_CLOUD_ENABLED", "1")
    monkeypatch.setenv("FUNDING_CLOUD_SSH_KEY", str(key))
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *_args, **_kwargs: CompletedProcess(
            [],
            0,
            stdout='{"statusCode":400,"data":{"detail":"invalid symbol"}}',
            stderr="",
        ),
    )

    with pytest.raises(client.FundingCloudError) as captured:
        client.funding_cloud_request("GET", "/v1/funding-formation")

    assert captured.value.status_code == 400
    assert str(captured.value) == "invalid symbol"


def test_cloud_status_reports_disabled_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUNDING_CLOUD_ENABLED", raising=False)
    assert client.funding_cloud_status() == {
        "enabled": False,
        "computeLocation": "local_paused",
        "status": "disabled",
    }

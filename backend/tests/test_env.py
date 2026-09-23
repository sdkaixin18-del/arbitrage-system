from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.env import load_project_env


def test_load_project_env_uses_private_explicit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text("TEST_PRIVATE_RUNTIME_VALUE=loaded\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("STOCK_REVIEW_ENV_FILE", str(env_file))
    monkeypatch.delenv("TEST_PRIVATE_RUNTIME_VALUE", raising=False)

    load_project_env()

    assert os.environ["TEST_PRIVATE_RUNTIME_VALUE"] == "loaded"


def test_load_project_env_rejects_insecure_explicit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text("TEST_PRIVATE_RUNTIME_VALUE=leaked\n", encoding="utf-8")
    env_file.chmod(0o644)
    monkeypatch.setenv("STOCK_REVIEW_ENV_FILE", str(env_file))

    with pytest.raises(RuntimeError, match="mode 600"):
        load_project_env()

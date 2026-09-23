from __future__ import annotations

import importlib.util
import os
import plistlib
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "supervisor.py"
SPEC = importlib.util.spec_from_file_location("stock_review_supervisor", MODULE_PATH)
assert SPEC and SPEC.loader
supervisor_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor_module
SPEC.loader.exec_module(supervisor_module)


def make_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_text: str = ""):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / ".env"
    env_file.write_text(env_text, encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("STOCK_REVIEW_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("STOCK_REVIEW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("STOCK_REVIEW_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(supervisor_module, "system_web_proxy", lambda: None)
    return supervisor_module.Supervisor()


def test_secrets_stay_in_private_file_and_are_not_in_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGACY_API_KEY", "inherited-secret")
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        "BACKEND_PORT=8123\nASTRO_SDK_API_KEY=file-secret\nBARK_WEBHOOK_URL=https://secret\n",
    )

    backend_env = instance.child_env("backend")
    frontend_env = instance.child_env("frontend")
    bridge_env = instance.child_env("astro-pulse-ssh")

    assert instance.backend_port == 8123
    assert "LEGACY_API_KEY" not in instance.settings
    assert "ASTRO_SDK_API_KEY" not in instance.settings
    assert backend_env["STOCK_REVIEW_ENV_FILE"] == str(tmp_path / "runtime/.env")
    assert "STOCK_REVIEW_ENV_FILE" not in frontend_env
    assert "STOCK_REVIEW_ENV_FILE" not in bridge_env
    assert "LEGACY_API_KEY" not in backend_env
    assert "ASTRO_SDK_API_KEY" not in backend_env
    assert "BARK_WEBHOOK_URL" not in backend_env


def test_current_services_receive_system_proxy_without_legacy_radar_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(tmp_path, monkeypatch)
    monkeypatch.setattr(supervisor_module, "system_web_proxy", lambda: "http://127.0.0.1:7890")

    backend_env = instance.child_env("backend")

    assert backend_env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "WRANGLER_WRITE_LOGS" not in backend_env
    assert "radar" not in instance.services


def test_retired_radar_proxy_does_not_override_current_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        "ARBITRAGE_RADAR_PROXY=http://127.0.0.1:7892\n",
    )
    monkeypatch.setattr(supervisor_module, "system_web_proxy", lambda: "http://127.0.0.1:7897")

    backend_env = instance.child_env("backend")

    assert backend_env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "ARBITRAGE_RADAR_PROXY" not in instance.settings


def test_astro_quote_bridge_is_opt_in_and_receives_only_connection_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "astro.pem"
    key.write_text("test-key", encoding="utf-8")
    key.chmod(0o600)
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        (
            "ASTRO_QUOTE_ENABLED=1\n"
            "ASTRO_QUOTE_BRIDGE_PORT=8877\n"
            "ASTRO_QUOTE_SSH_TARGET=ubuntu@example.com\n"
            f"ASTRO_QUOTE_SSH_KEY={key}\n"
            "ASTRO_SDK_API_KEY=must-not-leak\n"
        ),
    )

    assert "astro-quotes" in instance.services
    assert instance.services["astro-quotes"].port == 8877
    bridge_env = instance.child_env("astro-quotes")
    assert bridge_env["ASTRO_QUOTE_SSH_TARGET"] == "ubuntu@example.com"
    assert bridge_env["ASTRO_QUOTE_SSH_KEY"] == str(key)
    assert "ASTRO_SDK_API_KEY" not in bridge_env


def test_pulse_ssh_bridge_is_independent_and_receives_only_connection_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "pulse.pem"
    key.write_text("test-key", encoding="utf-8")
    key.chmod(0o600)
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        (
            "ASTRO_PULSE_SSH_ENABLED=1\n"
            "ASTRO_PULSE_SSH_PORT=8878\n"
            "ASTRO_PULSE_SSH_TARGET=ubuntu@example.com\n"
            f"ASTRO_PULSE_SSH_KEY={key}\n"
            "ASTRO_SDK_API_KEY=must-not-leak\n"
        ),
    )

    assert "astro-pulse-ssh" in instance.services
    assert instance.services["astro-pulse-ssh"].port == 8878
    assert "pulse_ssh_bridge.py" in instance.services["astro-pulse-ssh"].command[1]
    bridge_env = instance.child_env("astro-pulse-ssh")
    assert bridge_env["ASTRO_PULSE_SSH_TARGET"] == "ubuntu@example.com"
    assert bridge_env["ASTRO_PULSE_SSH_KEY"] == str(key)
    assert "ASTRO_SDK_API_KEY" not in bridge_env


def test_supervisor_rejects_env_file_readable_by_other_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / ".env"
    env_file.write_text("ASTRO_SDK_API_KEY=secret\n", encoding="utf-8")
    env_file.chmod(0o644)
    monkeypatch.setenv("STOCK_REVIEW_RUNTIME_ROOT", str(runtime))

    with pytest.raises(RuntimeError, match="权限不安全"):
        supervisor_module.Supervisor()


def test_single_slow_probe_does_not_restart_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        "SUPERVISOR_HEALTH_FAILURE_THRESHOLD=3\nSUPERVISOR_STARTUP_GRACE_SECONDS=0\n",
    )
    for state in instance.states.values():
        state.externally_managed = True
        state.grace_until = 0

    with (
        patch.object(supervisor_module, "port_open", return_value=True),
        patch.object(supervisor_module, "http_healthy", return_value=False),
        patch.object(instance, "recover_service") as recover,
    ):
        instance.supervise_once(now=100)

    recover.assert_not_called()
    assert all(state.consecutive_failures == 1 for state in instance.states.values())


def test_only_service_with_consecutive_failures_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        "SUPERVISOR_HEALTH_FAILURE_THRESHOLD=3\nSUPERVISOR_STARTUP_GRACE_SECONDS=0\n",
    )
    for state in instance.states.values():
        state.externally_managed = True
        state.grace_until = 0

    def health(url: str, *_args, **_kwargs) -> bool:
        return f":{instance.frontend_port}/" not in url

    with (
        patch.object(supervisor_module, "port_open", return_value=True),
        patch.object(supervisor_module, "http_healthy", side_effect=health),
        patch.object(instance, "recover_service") as recover,
    ):
        instance.supervise_once(now=100)
        instance.supervise_once(now=101)
        instance.supervise_once(now=102)

    recover.assert_called_once_with("frontend", "连续 3 次健康检查失败")
    assert instance.states["backend"].consecutive_failures == 0


def test_independent_recovery_stops_and_starts_only_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(tmp_path, monkeypatch)

    with (
        patch.object(instance, "stop_service") as stop_service,
        patch.object(instance, "start_service") as start_service,
    ):
        instance.recover_service("frontend", "test")

    stop_service.assert_called_once_with("frontend")
    start_service.assert_called_once_with("frontend")


def test_restart_backoff_increases_and_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(
        tmp_path,
        monkeypatch,
        "SUPERVISOR_RESTART_BASE_DELAY_SECONDS=2\nSUPERVISOR_RESTART_MAX_DELAY_SECONDS=5\n",
    )
    with patch.object(supervisor_module.time, "monotonic", return_value=100):
        instance.schedule_restart("backend", "first")
        first = instance.states["backend"].next_restart_at
        instance.schedule_restart("backend", "second")
        second = instance.states["backend"].next_restart_at
        instance.schedule_restart("backend", "third")
        third = instance.states["backend"].next_restart_at

    assert first == 102
    assert second == 104
    assert third == 105


def test_exited_child_recovers_without_touching_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(tmp_path, monkeypatch)
    exited = Mock()
    exited.poll.return_value = 7
    exited.returncode = 7
    instance.children["frontend"] = exited
    instance.states["backend"].externally_managed = True

    with (
        patch.object(supervisor_module, "port_open", return_value=True),
        patch.object(supervisor_module, "http_healthy", return_value=True),
        patch.object(instance, "recover_service") as recover,
    ):
        instance.supervise_once(now=100)

    recover.assert_called_once_with("frontend", "子进程退出，退出码 7")


def test_launchagent_template_never_embeds_credentials() -> None:
    template = MODULE_PATH.with_name("com.stock-review-mac.autostart.plist.template")
    payload = plistlib.loads(template.read_bytes())
    environment = payload.get("EnvironmentVariables") or {}

    assert environment == {}
    assert not any(supervisor_module.is_sensitive_env_key(key) for key in environment)
    arguments = payload.get("ProgramArguments") or []
    assert arguments[:2] == ["/usr/bin/env", "-i"]
    assert "STOCK_REVIEW_RUNTIME_ROOT=__RUNTIME_ROOT__" in arguments
    assert "STOCK_REVIEW_DATA_ROOT=__DATA_ROOT__" in arguments


def test_unhealthy_external_process_is_never_terminated_or_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(tmp_path, monkeypatch)
    instance.states["frontend"].externally_managed = True
    instance.states["frontend"].consecutive_failures = instance.failure_threshold

    with (
        patch.object(supervisor_module, "port_open", return_value=True),
        patch.object(instance, "stop_service") as stop_service,
        patch.object(instance, "start_service") as start_service,
    ):
        instance.recover_service("frontend", "external unhealthy")

    stop_service.assert_not_called()
    start_service.assert_not_called()
    assert instance.states["frontend"].externally_managed is True
    assert instance.states["frontend"].consecutive_failures == 0


def test_released_external_port_is_safely_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_supervisor(tmp_path, monkeypatch)
    for state in instance.states.values():
        state.externally_managed = True
        state.grace_until = 0

    def port_is_open(port: int) -> bool:
        return port != instance.frontend_port

    with (
        patch.object(supervisor_module, "port_open", side_effect=port_is_open),
        patch.object(supervisor_module, "http_healthy", return_value=True),
        patch.object(instance, "start_service") as start_service,
    ):
        instance.supervise_once(now=100)

    start_service.assert_called_once_with("frontend")
    assert instance.states["frontend"].externally_managed is False


def test_retired_radar_is_omitted_without_stopping_siblings(tmp_path, monkeypatch):
    instance = make_supervisor(tmp_path, monkeypatch)
    instance.launcher_root = tmp_path
    (tmp_path / 'radar.disabled').write_text('disabled by user')
    services = instance._service_specs()
    assert 'radar' not in services
    assert {'backend', 'frontend'} <= services.keys()
    (tmp_path / 'radar.disabled').unlink()
    assert 'radar' not in instance._service_specs()

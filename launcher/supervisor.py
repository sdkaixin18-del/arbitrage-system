#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_RUNTIME_ROOT = Path("/home/example/Documents/套利系统/runtime/app")
DEFAULT_DATA_ROOT = Path("/home/example/Documents/套利系统")
DEFAULT_LOG_DIR = Path("/home/example/Library/Logs/stock-review-mac")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
SUPERVISOR_SETTING_KEYS = {
    "APP_ENV", "BACKEND_PORT", "FRONTEND_HOST", "FRONTEND_PORT",
    "LLM_PROVIDER", "NODE_USE_ENV_PROXY", "SUPERVISOR_HEALTH_FAILURE_THRESHOLD",
    "SUPERVISOR_HEALTH_INTERVAL_SECONDS", "SUPERVISOR_HEALTH_TIMEOUT_SECONDS",
    "SUPERVISOR_RESTART_BASE_DELAY_SECONDS", "SUPERVISOR_RESTART_MAX_DELAY_SECONDS",
    "SUPERVISOR_STARTUP_GRACE_SECONDS", "ASTRO_QUOTE_ENABLED", "ASTRO_QUOTE_BRIDGE_PORT",
    "ASTRO_QUOTE_POLL_MS", "ASTRO_QUOTE_MAX_AGE_MS", "ASTRO_QUOTE_LEASE_SECONDS", "ASTRO_QUOTE_SSH_TARGET",
    "ASTRO_QUOTE_SSH_KEY", "ASTRO_QUOTE_CONTAINER", "ASTRO_QUOTE_GATEWAY_PATH",
    "ASTRO_MANUAL_ORDER_SSH_TARGET", "ASTRO_MANUAL_ORDER_SSH_KEY",
    "ASTRO_MANUAL_ORDER_CONTAINER",
    "ASTRO_PULSE_SSH_ENABLED", "ASTRO_PULSE_SSH_PORT", "ASTRO_PULSE_SSH_TARGET", "ASTRO_PULSE_SSH_KEY",
}
SENSITIVE_ENV_TOKENS = (
    "API_KEY", "AUTH_TOKEN", "BARK_URL", "COOKIE", "CREDENTIAL", "PASSWORD", "PRIVATE_KEY",
    "SECRET", "WEBHOOK_URL",
)
SAFE_INHERITED_ENV_KEYS = {
    "HOME", "LANG", "LOGNAME", "NO_PROXY", "SHELL", "SSH_AUTH_SOCK", "TMPDIR", "USER",
    "XPC_FLAGS", "XPC_SERVICE_NAME", "__CF_USER_TEXT_ENCODING",
}
def is_sensitive_env_key(key: str) -> bool:
    normalized = key.upper()
    return any(token in normalized for token in SENSITIVE_ENV_TOKENS)


def load_dotenv(path: Path, env: dict[str, str], allowed_keys: set[str] | None = None) -> None:
    """Load simple KEY=VALUE entries without executing shell code."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if allowed_keys is not None and key not in allowed_keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value


def private_env_file(path: Path) -> bool:
    """Only trust a regular, current-user-owned file with no group/other access."""
    try:
        stat = path.stat()
    except OSError:
        return False
    return path.is_file() and stat.st_uid == os.getuid() and stat.st_mode & 0o077 == 0


def read_number(settings: dict[str, str], key: str, default: float, minimum: float) -> float:
    try:
        value = float(settings.get(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def system_web_proxy() -> str | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    def value(key: str) -> str | None:
        match = re.search(rf"^\s*{re.escape(key)}\s*:\s*(\S+)\s*$", result.stdout, re.MULTILINE)
        return match.group(1) if match else None

    if value("HTTPSEnable") != "1":
        return None
    host = value("HTTPSProxy")
    port = value("HTTPSPort")
    if not host or not port or not port.isdigit():
        return None
    return f"http://{host}:{port}"


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def http_healthy(url: str, marker: str | None = None, timeout: float = 2.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return False
            if marker is None:
                return True
            payload = response.read(32 * 1024).decode("utf-8", errors="ignore")
            return marker in payload
    except (OSError, URLError, TimeoutError):
        return False


def rotate_log(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < LOG_MAX_BYTES:
            return
        path.with_name(f"{path.name}.{LOG_BACKUP_COUNT}").unlink(missing_ok=True)
        for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if source.exists():
                source.replace(target)
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        return


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    port: int
    command: list[str]
    cwd: Path
    log_name: str
    health_url: str
    marker: str
    startup_timeout: float


@dataclass
class ServiceState:
    consecutive_failures: int = 0
    restart_attempts: int = 0
    next_restart_at: float = 0.0
    grace_until: float = 0.0
    externally_managed: bool = False


class Supervisor:
    def __init__(self) -> None:
        self.launcher_root = Path(__file__).resolve().parent
        self.runtime_root = Path(os.environ.get("STOCK_REVIEW_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT)).expanduser()
        self.data_root = Path(os.environ.get("STOCK_REVIEW_DATA_ROOT", DEFAULT_DATA_ROOT)).expanduser()
        self.log_dir = Path(os.environ.get("STOCK_REVIEW_LOG_DIR", DEFAULT_LOG_DIR)).expanduser()

        launcher_env = self.launcher_root / "runtime.env"
        runtime_env = self.runtime_root / ".env"
        self.env_file = launcher_env if launcher_env.is_file() else runtime_env
        if self.env_file.exists() and not private_env_file(self.env_file):
            raise RuntimeError(f"运行配置文件权限不安全，必须仅当前用户可读写：{self.env_file}")

        # Only non-sensitive launcher settings enter this process. Secrets remain
        # in the mode-600 file and are loaded by the backend itself.
        self.settings = {
            key: os.environ[key]
            for key in SUPERVISOR_SETTING_KEYS
            if key in os.environ
        }
        load_dotenv(self.env_file, self.settings, SUPERVISOR_SETTING_KEYS)
        self.backend_port = int(self.settings.get("BACKEND_PORT", "8000"))
        self.frontend_port = int(self.settings.get("FRONTEND_PORT", "5173"))
        self.astro_quote_enabled = self.settings.get("ASTRO_QUOTE_ENABLED", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self.astro_quote_port = int(self.settings.get("ASTRO_QUOTE_BRIDGE_PORT", "8765"))
        self.astro_pulse_ssh_enabled = self.settings.get("ASTRO_PULSE_SSH_ENABLED", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self.astro_pulse_ssh_port = int(self.settings.get("ASTRO_PULSE_SSH_PORT", "8766"))
        self.frontend_host = self.settings.get("FRONTEND_HOST", "127.0.0.1")
        self.health_interval = read_number(self.settings, "SUPERVISOR_HEALTH_INTERVAL_SECONDS", 15.0, 1.0)
        self.health_timeout = read_number(self.settings, "SUPERVISOR_HEALTH_TIMEOUT_SECONDS", 8.0, 1.0)
        self.failure_threshold = int(read_number(
            self.settings, "SUPERVISOR_HEALTH_FAILURE_THRESHOLD", 3.0, 2.0
        ))
        self.startup_grace = read_number(self.settings, "SUPERVISOR_STARTUP_GRACE_SECONDS", 30.0, 0.0)
        self.restart_base_delay = read_number(
            self.settings, "SUPERVISOR_RESTART_BASE_DELAY_SECONDS", 2.0, 0.5
        )
        self.restart_max_delay = read_number(
            self.settings, "SUPERVISOR_RESTART_MAX_DELAY_SECONDS", 60.0, self.restart_base_delay
        )
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.log_handles: dict[str, BinaryIO] = {}
        self.stopping = False
        self.stop_event = threading.Event()
        self.services = self._service_specs()
        self.states = {name: ServiceState() for name in self.services}

    def _service_specs(self) -> dict[str, ServiceSpec]:
        services: dict[str, ServiceSpec] = {}
        if self.astro_pulse_ssh_enabled:
            services["astro-pulse-ssh"] = ServiceSpec(
                "astro-pulse-ssh", self.astro_pulse_ssh_port,
                ["/usr/bin/python3", str(self.launcher_root / "pulse_ssh_bridge.py"),
                 "--host", "127.0.0.1", "--port", str(self.astro_pulse_ssh_port)],
                self.launcher_root, "pulse-ssh-bridge.log",
                f"http://127.0.0.1:{self.astro_pulse_ssh_port}/health", '"status":"ok"', 15,
            )
        if self.astro_quote_enabled:
            services["astro-quotes"] = ServiceSpec(
                "astro-quotes", self.astro_quote_port,
                ["/usr/bin/python3", str(self.launcher_root / "astro_quote_bridge.py"),
                 "--host", "127.0.0.1", "--port", str(self.astro_quote_port)],
                self.launcher_root, "astro-quote-bridge.log",
                f"http://127.0.0.1:{self.astro_quote_port}/health", '"status":"ok"', 30,
            )
        services.update({
            "backend": ServiceSpec(
                "backend", self.backend_port,
                [str(self.runtime_root / "backend/.venv/bin/python"), "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(self.backend_port), "--no-access-log"],
                self.runtime_root / "backend", "backend-supervisor.log",
                f"http://127.0.0.1:{self.backend_port}/api/health", '"status":"ok"', 45,
            ),
            "frontend": ServiceSpec(
                "frontend", self.frontend_port,
                ["/usr/bin/python3", str(self.launcher_root / "frontend_static_server.py"),
                 "--host", self.frontend_host, "--port", str(self.frontend_port),
                 "--backend", f"http://127.0.0.1:{self.backend_port}",
                 "--dist", str(self.launcher_root / "frontend-dist")],
                self.launcher_root, "frontend-supervisor.log",
                f"http://127.0.0.1:{self.frontend_port}/", '<div id="root"></div>', 45,
            ),
        })
        return services

    def child_env(self, service_name: str) -> dict[str, str]:
        # Use an allowlist instead of forwarding the launchd/shell environment.
        # This also removes secrets with unusual names that a token blacklist
        # could miss. Application settings are read from the private env file.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in SAFE_INHERITED_ENV_KEYS or key.startswith("LC_")
        }
        detected_proxy = system_web_proxy()
        if detected_proxy:
            env.setdefault("CRYPTO_API_PROXY", detected_proxy)
            env.setdefault("HTTP_PROXY", detected_proxy)
            env.setdefault("HTTPS_PROXY", detected_proxy)
            env.setdefault("ALL_PROXY", detected_proxy)
        env.update({
            "PATH": (f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                     f"/usr/sbin:/sbin:{env.get('PATH', '')}"),
            "STOCK_REVIEW_DATA_DIR": str(self.data_root / "site-data"),
            "STOCK_REVIEW_DATA_ROOT": str(self.data_root),
            "STOCK_REVIEW_REQUIRE_EXTERNAL_DRIVE": "0",
            "MARKET_REVIEW_REPORT_DIR": str(self.data_root / "reports/market-review"),
            "APP_ENV": self.settings.get("APP_ENV", "local"),
            "LLM_PROVIDER": self.settings.get("LLM_PROVIDER", "mock"),
            "BACKEND_PORT": str(self.backend_port),
            "FRONTEND_PORT": str(self.frontend_port),
            "FRONTEND_HOST": self.frontend_host,
            "NODE_ENV": "production",
            "NODE_USE_ENV_PROXY": self.settings.get("NODE_USE_ENV_PROXY", "1"),
        })
        if service_name == "astro-quotes":
            for key in (
                "ASTRO_QUOTE_BRIDGE_PORT", "ASTRO_QUOTE_POLL_MS", "ASTRO_QUOTE_MAX_AGE_MS",
                "ASTRO_QUOTE_LEASE_SECONDS",
                "ASTRO_QUOTE_SSH_TARGET", "ASTRO_QUOTE_SSH_KEY", "ASTRO_QUOTE_CONTAINER",
                "ASTRO_QUOTE_GATEWAY_PATH", "ASTRO_MANUAL_ORDER_SSH_TARGET",
                "ASTRO_MANUAL_ORDER_SSH_KEY", "ASTRO_MANUAL_ORDER_CONTAINER",
            ):
                if key in self.settings:
                    env[key] = self.settings[key]
        if service_name == "astro-pulse-ssh":
            for key in (
                "ASTRO_PULSE_SSH_PORT", "ASTRO_PULSE_SSH_TARGET", "ASTRO_PULSE_SSH_KEY",
            ):
                if key in self.settings:
                    env[key] = self.settings[key]
        if service_name == "backend" and self.env_file.is_file():
            env["STOCK_REVIEW_ENV_FILE"] = str(self.env_file)
        return env

    def open_log(self, name: str, log_name: str) -> BinaryIO:
        self.close_log(name)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / log_name
        rotate_log(path)
        handle = path.open("ab", buffering=0)
        self.log_handles[name] = handle
        return handle

    def close_log(self, name: str) -> None:
        handle = self.log_handles.pop(name, None)
        if handle is not None:
            handle.close()

    def start_service(self, name: str) -> None:
        spec = self.services[name]
        state = self.states[name]
        if port_open(spec.port):
            state.externally_managed = name not in self.children
            if not http_healthy(spec.health_url, spec.marker, timeout=self.health_timeout):
                raise RuntimeError(f"{name} 端口 {spec.port} 已占用，但业务健康检查失败")
            state.consecutive_failures = state.restart_attempts = 0
            state.next_restart_at = 0
            state.grace_until = time.monotonic() + self.startup_grace
            print(f"{name} already healthy: {spec.health_url}", flush=True)
            return

        handle = self.open_log(name, spec.log_name)
        child = subprocess.Popen(
            spec.command, cwd=str(spec.cwd), env=self.child_env(name), stdout=handle, stderr=handle,
        )
        self.children[name] = child
        state.externally_managed = False
        deadline = time.monotonic() + spec.startup_timeout
        while time.monotonic() < deadline and not self.stopping:
            code = child.poll()
            if code is not None:
                self.children.pop(name, None)
                self.close_log(name)
                raise RuntimeError(f"{name} 启动失败，退出码 {code}")
            if http_healthy(spec.health_url, spec.marker, timeout=min(self.health_timeout, 3.0)):
                state.consecutive_failures = state.restart_attempts = 0
                state.next_restart_at = 0
                state.grace_until = time.monotonic() + self.startup_grace
                print(f"{name} ready: {spec.health_url}", flush=True)
                return
            self.stop_event.wait(0.5)
        if not self.stopping:
            self.stop_service(name)
            raise RuntimeError(f"{name} 启动超时：{spec.health_url}")

    def stop_service(self, name: str) -> None:
        child = self.children.pop(name, None)
        if child is not None and child.poll() is None:
            child.terminate()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.2)
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.close_log(name)

    def request_stop(self, *_args: object) -> None:
        self.stopping = True
        self.stop_event.set()

    def shutdown(self) -> None:
        self.request_stop()
        for name in list(self.children):
            self.stop_service(name)
        for name in list(self.log_handles):
            self.close_log(name)

    def schedule_restart(self, name: str, reason: str) -> None:
        state = self.states[name]
        state.restart_attempts += 1
        exponent = min(state.restart_attempts - 1, 16)
        delay = min(self.restart_max_delay, self.restart_base_delay * (2 ** exponent))
        state.next_restart_at = time.monotonic() + delay
        state.consecutive_failures = 0
        print(f"{name} 独立恢复暂未成功：{reason}；{delay:.1f} 秒后重试", file=sys.stderr, flush=True)

    def recover_service(self, name: str, reason: str) -> None:
        if self.stopping:
            return
        state = self.states[name]
        spec = self.services[name]
        if state.externally_managed and name not in self.children and port_open(spec.port):
            # Never kill or race a process this supervisor did not create. Keep
            # observing it; if it recovers the probe counter resets, and if it
            # releases the port supervise_once safely starts our own service.
            state.consecutive_failures = 0
            print(
                f"{name} 由外部进程占用且健康异常；等待其恢复或释放端口，不执行终止",
                file=sys.stderr,
                flush=True,
            )
            return
        print(f"{name} 独立恢复：{reason}", file=sys.stderr, flush=True)
        self.stop_service(name)
        state.externally_managed = False
        try:
            self.start_service(name)
        except Exception as exc:
            self.schedule_restart(name, str(exc))

    def supervise_once(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        for name, spec in self.services.items():
            if self.stopping:
                return
            state = self.states[name]
            child = self.children.get(name)
            if child is not None and child.poll() is not None:
                exit_code = child.returncode
                self.children.pop(name, None)
                self.close_log(name)
                self.recover_service(name, f"子进程退出，退出码 {exit_code}")
                continue
            if child is None and state.externally_managed and not port_open(spec.port):
                state.externally_managed = False
                try:
                    self.start_service(name)
                except Exception as exc:
                    self.schedule_restart(name, str(exc))
                continue
            if child is None and not state.externally_managed:
                if current >= state.next_restart_at:
                    try:
                        self.start_service(name)
                    except Exception as exc:
                        self.schedule_restart(name, str(exc))
                continue
            if current < state.grace_until:
                continue
            if http_healthy(spec.health_url, spec.marker, timeout=self.health_timeout):
                if state.consecutive_failures:
                    print(f"{name} 健康检查已恢复", flush=True)
                state.consecutive_failures = 0
                state.restart_attempts = 0
                continue
            state.consecutive_failures += 1
            print(f"{name} 健康检查失败 {state.consecutive_failures}/{self.failure_threshold}",
                  file=sys.stderr, flush=True)
            if state.consecutive_failures >= self.failure_threshold:
                self.recover_service(name, f"连续 {self.failure_threshold} 次健康检查失败")

    def validate_installation(self) -> None:
        if not self.runtime_root.is_dir():
            raise RuntimeError(f"套利系统运行目录不存在：{self.runtime_root}")
        if not (self.launcher_root / "frontend-dist/index.html").is_file():
            raise RuntimeError("主前端生产构建不存在，请先执行统一部署脚本")
        if self.astro_quote_enabled and not (self.launcher_root / "astro_quote_bridge.py").is_file():
            raise RuntimeError("Astro 行情桥不存在，请先执行统一部署脚本")

    def run(self) -> int:
        self.validate_installation()
        manifest_path = self.launcher_root / "deployment-manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"deployment: {manifest.get('deploymentId') or 'unknown'}", flush=True)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        # Failed components remain on their own retry schedule. Healthy siblings
        # are never torn down because a single probe was slow.
        for name in self.services:
            if self.stopping:
                break
            try:
                self.start_service(name)
            except Exception as exc:
                self.schedule_restart(name, str(exc))
        while not self.stopping:
            self.supervise_once()
            self.stop_event.wait(self.health_interval)
        self.shutdown()
        return 0


def main() -> int:
    supervisor: Supervisor | None = None
    try:
        supervisor = Supervisor()
        return supervisor.run()
    except Exception as exc:
        print(f"supervisor failed: {exc}", file=sys.stderr, flush=True)
        if supervisor is not None:
            supervisor.shutdown()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

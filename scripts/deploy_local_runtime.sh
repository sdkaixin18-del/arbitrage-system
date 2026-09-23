#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="/home/example/Documents/套利系统/runtime/app"
DATA_ROOT="/home/example/Documents/套利系统"
LAUNCHER_ROOT="$HOME/Library/Application Support/stock-review-mac-launcher"
LAUNCHER_SECRET_DIR="$LAUNCHER_ROOT/secrets"
ASTRO_KEY_SOURCE="/home/example/Documents/套利系统/runtime/secrets/astro.pem"
ASTRO_KEY_DEST="$LAUNCHER_SECRET_DIR/astro.pem"
LOG_DIR="$HOME/Library/Logs/stock-review-mac"
LABEL="com.stock-review-mac.autostart"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RESTART=1

for argument in "$@"; do
  case "$argument" in
    --no-restart) RESTART=0 ;;
    --install-launchagent) ;;
    *) echo "未知参数：$argument" >&2; exit 2 ;;
  esac
done

if [ "$SOURCE_ROOT" != "/home/example/Documents/套利系统/source/stock-review-mac" ]; then
  echo "源码路径异常，拒绝部署：$SOURCE_ROOT" >&2
  exit 1
fi
if [ "$RUNTIME_ROOT" != "/home/example/Documents/套利系统/runtime/app" ]; then
  echo "运行路径异常，拒绝部署：$RUNTIME_ROOT" >&2
  exit 1
fi

mkdir -p "$RUNTIME_ROOT" "$LAUNCHER_ROOT" "$LAUNCHER_SECRET_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
if [ -f "$ASTRO_KEY_SOURCE" ]; then
  install -m 600 "$ASTRO_KEY_SOURCE" "$ASTRO_KEY_DEST"
fi

echo "[1/6] 构建主前端"
(cd "$SOURCE_ROOT/frontend" && npm run build)

job_was_stopped=0
recover_launchagent() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$job_was_stopped" -eq 1 ] && [ -f "$PLIST" ]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap recover_launchagent EXIT

if [ "$RESTART" -eq 1 ]; then
  echo "[3/6] 短暂停止当前服务"
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  job_was_stopped=1
fi

echo "[4/6] 同步源码、运行副本和启动器"
old_backend_requirements_hash="$(shasum -a 256 "$RUNTIME_ROOT/backend/requirements.txt" 2>/dev/null | awk '{print $1}' || true)"
RSYNC_EXCLUDES=(
  --exclude ".git"
  --exclude ".env"
  --exclude ".DS_Store"
  --exclude "._*"
  --exclude "*.bak"
  --exclude "*.bak-*"
  --exclude "__pycache__"
  --exclude ".pytest_cache"
  --exclude "backend/.venv"
  --exclude "frontend/node_modules"
  --exclude "exchange-news-site/node_modules"
  --exclude "exchange-news-worker/node_modules"
  --exclude "exchange-news-site/dist"
  --exclude "exchange-news-worker/dist"
  --exclude "exchange-news-worker/.wrangler"
  --exclude "exchange-news-worker/.dev.vars"
  --exclude ".run-logs"
  --exclude "deployment-manifest.json"
  --exclude "analysis"
  --exclude "artifacts"
)
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SOURCE_ROOT/" "$RUNTIME_ROOT/"

if [ ! -f "$RUNTIME_ROOT/.env" ]; then
  install -m 600 "$SOURCE_ROOT/.env" "$RUNTIME_ROOT/.env"
fi
chmod 600 "$RUNTIME_ROOT/.env"

install -m 755 "$SOURCE_ROOT/launcher/supervisor.py" "$LAUNCHER_ROOT/supervisor.py"
install -m 755 "$SOURCE_ROOT/launcher/astro_quote_bridge.py" "$LAUNCHER_ROOT/astro_quote_bridge.py"
install -m 755 "$SOURCE_ROOT/launcher/pulse_ssh_bridge.py" "$LAUNCHER_ROOT/pulse_ssh_bridge.py"
install -m 755 "$SOURCE_ROOT/frontend-static-server.py" "$LAUNCHER_ROOT/frontend_static_server.py"
install -m 600 "$RUNTIME_ROOT/.env" "$LAUNCHER_ROOT/runtime.env"
chmod 600 "$LAUNCHER_ROOT/runtime.env"
rm -f "$LAUNCHER_ROOT/start.sh"
rsync -a --delete "$SOURCE_ROOT/frontend/dist/" "$LAUNCHER_ROOT/frontend-dist/"
new_backend_requirements_hash="$(shasum -a 256 "$RUNTIME_ROOT/backend/requirements.txt" | awk '{print $1}')"
backend_venv_created=0
if [ ! -x "$RUNTIME_ROOT/backend/.venv/bin/python" ]; then
  python3 -m venv "$RUNTIME_ROOT/backend/.venv"
  backend_venv_created=1
fi
if [ "$backend_venv_created" -eq 1 ] || [ "$old_backend_requirements_hash" != "$new_backend_requirements_hash" ]; then
  "$RUNTIME_ROOT/backend/.venv/bin/python" -m pip install -r "$RUNTIME_ROOT/backend/requirements.txt"
fi
python3 - "$SOURCE_ROOT/launcher/com.stock-review-mac.autostart.plist.template" "$PLIST" \
  "$LAUNCHER_ROOT" "$RUNTIME_ROOT" "$DATA_ROOT" "$LOG_DIR" "$HOME" <<'PY'
from pathlib import Path
import sys

template, output, launcher, runtime, data, logs, user_home = map(Path, sys.argv[1:])
text = template.read_text(encoding="utf-8")
replacements = {
    "__LAUNCHER_ROOT__": str(launcher),
    "__RUNTIME_ROOT__": str(runtime),
    "__DATA_ROOT__": str(data),
    "__LOG_DIR__": str(logs),
    "__USER_HOME__": str(user_home),
}
for key, value in replacements.items():
    text = text.replace(key, value)
output.write_text(text, encoding="utf-8")
PY
plutil -lint "$PLIST" >/dev/null

# Credentials must stay in the private env file. The LaunchAgent plist may only
# contain paths and other non-sensitive bootstrap settings.
python3 - "$PLIST" "$LAUNCHER_ROOT/runtime.env" <<'PY'
from pathlib import Path
import os
import plistlib
import stat
import sys

plist_path, env_path = map(Path, sys.argv[1:])
env_mode = stat.S_IMODE(env_path.stat().st_mode)
if env_path.stat().st_uid != os.getuid() or env_mode != 0o600:
    raise SystemExit(f"运行配置文件权限必须为 600：{env_path}")

payload = plistlib.loads(plist_path.read_bytes())
environment = payload.get("EnvironmentVariables") or {}
sensitive_tokens = ("API_KEY", "AUTH_TOKEN", "COOKIE", "CREDENTIAL", "PASSWORD", "PRIVATE_KEY", "SECRET", "WEBHOOK_URL")
leaked = [key for key in environment if any(token in key.upper() for token in sensitive_tokens)]
if leaked:
    raise SystemExit("LaunchAgent 环境中禁止写入敏感配置：" + ", ".join(sorted(leaked)))
PY

echo "[5/6] 生成部署版本清单"
deploy_tmp="$(mktemp -d -t stock-review-deployment)"
manifest_tmp="$deploy_tmp/deployment-manifest.json"
python3 - "$SOURCE_ROOT" "$RUNTIME_ROOT" "$LAUNCHER_ROOT" "$manifest_tmp" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import subprocess
import sys

source, runtime, launcher, output = map(Path, sys.argv[1:])

def command(*args: str) -> str | None:
    result = subprocess.run(args, cwd=source, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None

def digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

created_at = datetime.now(timezone.utc)
commit = command("git", "rev-parse", "--short=12", "HEAD") or "unversioned"
dirty = bool(command("git", "status", "--porcelain"))
deployment_id = f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{commit}"
manifest = {
    "deploymentId": deployment_id,
    "deployedAt": created_at.isoformat(),
    "sourceRoot": str(source),
    "runtimeRoot": str(runtime),
    "launcherRoot": str(launcher),
    "gitCommit": commit,
    "gitDirty": dirty,
    "files": {
        "backendCrypto": digest(runtime / "backend/app/crypto.py"),
        "backendModels": digest(runtime / "backend/app/models.py"),
        "frontendIndex": digest(launcher / "frontend-dist/index.html"),
        "supervisor": digest(launcher / "supervisor.py"),
        "astroQuoteBridge": digest(launcher / "astro_quote_bridge.py"),
        "pulseSshBridge": digest(launcher / "pulse_ssh_bridge.py"),
    },
}
output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(deployment_id)
PY
install -m 644 "$manifest_tmp" "$RUNTIME_ROOT/deployment-manifest.json"
install -m 644 "$manifest_tmp" "$LAUNCHER_ROOT/deployment-manifest.json"
rm -rf "$deploy_tmp"

if [ "$RESTART" -eq 1 ]; then
  echo "[6/6] 启动并验证服务"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  job_was_stopped=0

python3 - "$RUNTIME_ROOT/.env" <<'PY'
from pathlib import Path
import sys
import time
from urllib.request import urlopen

checks = {
    "backend": ("http://127.0.0.1:8000/api/health", '"status":"ok"'),
    "frontend": ("http://127.0.0.1:5173/", '<div id="root"></div>'),
}
env_text = Path(sys.argv[1]).read_text(encoding="utf-8")
enabled = any(
    line.strip().lower() in {"astro_quote_enabled=1", "astro_quote_enabled=true", "astro_quote_enabled=yes", "astro_quote_enabled=on"}
    for line in env_text.splitlines()
)
if enabled:
    checks["astro-quotes"] = ("http://127.0.0.1:8765/health", '"status":"ok"')
settings = dict(
    line.strip().split("=", 1)
    for line in env_text.splitlines()
    if "=" in line and not line.strip().startswith("#")
)
if settings.get("ASTRO_PULSE_SSH_ENABLED", "0").strip().strip("\"'").lower() in {"1", "true", "yes", "on"}:
    pulse_port = int(settings.get("ASTRO_PULSE_SSH_PORT", "8766").strip().strip("\"'"))
    checks["astro-pulse-ssh"] = (f"http://127.0.0.1:{pulse_port}/health", '"status":"ok"')
deadline = time.monotonic() + 120
pending = dict(checks)
while pending and time.monotonic() < deadline:
    for name, (url, marker) in list(pending.items()):
        try:
            with urlopen(url, timeout=3) as response:
                body = response.read(65536).decode("utf-8", errors="ignore")
            if 200 <= response.status < 300 and marker in body:
                del pending[name]
        except Exception:
            pass
    if pending:
        time.sleep(1)
if pending:
    raise SystemExit("服务验证失败：" + ", ".join(pending))
print("全部服务均已通过健康检查")
PY
else
  echo "[6/6] 已按 --no-restart 完成同步"
fi

trap - EXIT
echo "统一部署完成。"

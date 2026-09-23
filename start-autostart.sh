#!/usr/bin/env bash
set -euo pipefail

LAUNCHER_ROOT="$HOME/Library/Application Support/stock-review-mac-launcher"
SUPERVISOR="$LAUNCHER_ROOT/supervisor.py"

if [ ! -f "$SUPERVISOR" ]; then
  echo "统一启动器不存在：$SUPERVISOR" >&2
  echo "请先运行“安装开机自动启动.command”。" >&2
  exit 1
fi

exec /usr/bin/python3 "$SUPERVISOR"

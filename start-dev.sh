#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
DATA_ROOT="${STOCK_REVIEW_DATA_ROOT:-$(cd "$ROOT/../.." && pwd)}"
export STOCK_REVIEW_REQUIRE_EXTERNAL_DRIVE="${STOCK_REVIEW_REQUIRE_EXTERNAL_DRIVE:-0}"
export STOCK_REVIEW_DATA_DIR="${STOCK_REVIEW_DATA_DIR:-$DATA_ROOT/site-data}"
export MARKET_REVIEW_REPORT_DIR="${MARKET_REVIEW_REPORT_DIR:-$DATA_ROOT/reports/market-review}"
export APP_ENV="${APP_ENV:-local}"
export LLM_PROVIDER="${LLM_PROVIDER:-mock}"
export BACKEND_PORT="${BACKEND_PORT:-8000}"
export FRONTEND_PORT="${FRONTEND_PORT:-5173}"
export FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
export NODE_USE_ENV_PROXY="${NODE_USE_ENV_PROXY:-1}"
if [ -z "${FRONTEND_BASE_URL:-}" ]; then
  FRONTEND_LAN_IP="${FRONTEND_LAN_IP:-}"
  if [ -z "$FRONTEND_LAN_IP" ] && command -v ipconfig >/dev/null 2>&1; then
    FRONTEND_LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  fi
  if [ -z "$FRONTEND_LAN_IP" ] && command -v ipconfig >/dev/null 2>&1; then
    FRONTEND_LAN_IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  export FRONTEND_BASE_URL="http://${FRONTEND_LAN_IP:-127.0.0.1}:$FRONTEND_PORT"
else
  export FRONTEND_BASE_URL
fi

PYTHON_BIN="${PYTHON_BIN:-python3.12}"

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

backend_healthy() {
  curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1
}

frontend_healthy() {
  curl -fsS "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1
}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Missing Python 3.12. Install it or set PYTHON_BIN=/path/to/python3.12" >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Missing Node/npm. Install Node 20+ or ensure it is on PATH." >&2
  exit 1
fi

mkdir -p "$STOCK_REVIEW_DATA_DIR" "$MARKET_REVIEW_REPORT_DIR"

cd "$ROOT/backend"
if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --disable-pip-version-check -r requirements.txt

cd "$ROOT/frontend"
if [ ! -d "node_modules" ]; then
  npm install
fi

cd "$ROOT/backend"
source .venv/bin/activate
BACKEND_PID=""
FRONTEND_PID=""
STARTED_PIDS=()

if port_in_use "$BACKEND_PORT"; then
  if backend_healthy; then
    echo "Backend already running: http://127.0.0.1:$BACKEND_PORT/api/health"
  else
    echo "Backend port $BACKEND_PORT is occupied, but health check failed. Please close the old backend process and run again." >&2
    exit 1
  fi
else
  python -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload &
  BACKEND_PID=$!
  STARTED_PIDS+=("$BACKEND_PID")
fi

cd "$ROOT/frontend"
if port_in_use "$FRONTEND_PORT"; then
  if frontend_healthy; then
    echo "Frontend already running: http://127.0.0.1:$FRONTEND_PORT"
  else
    echo "Frontend port $FRONTEND_PORT is occupied, but the page is not responding. Keeping backend running; close the old frontend process and run this script again if the page cannot open." >&2
  fi
else
  npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
  FRONTEND_PID=$!
  STARTED_PIDS+=("$FRONTEND_PID")
fi

echo "Frontend: http://127.0.0.1:$FRONTEND_PORT"
echo "Frontend public: $FRONTEND_BASE_URL"
echo "Backend:  http://127.0.0.1:$BACKEND_PORT/api/health"

cleanup() {
  if [ "${#STARTED_PIDS[@]}" -gt 0 ]; then
    kill "${STARTED_PIDS[@]}" 2>/dev/null || true
  fi
}

trap cleanup EXIT
if [ "${#STARTED_PIDS[@]}" -eq 0 ]; then
  echo "Frontend and backend are already running. Press CTRL-C to stop watching."
  while true; do
    sleep 3600
  done
fi

wait "${STARTED_PIDS[@]}"

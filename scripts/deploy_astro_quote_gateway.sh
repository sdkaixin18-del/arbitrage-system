#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_KEY="${ASTRO_QUOTE_SSH_KEY:-${ASTRO_MANUAL_ORDER_SSH_KEY:-/home/example/Downloads/astro.pem}}"
SSH_TARGET="${ASTRO_QUOTE_SSH_TARGET:-${ASTRO_MANUAL_ORDER_SSH_TARGET:-ubuntu@192.0.2.10}}"
CONTAINER="${ASTRO_QUOTE_CONTAINER:-${ASTRO_MANUAL_ORDER_CONTAINER:-astro-app}}"
SOURCE="$ROOT/scripts/astro_quote_gateway.cjs"
REMOTE_TMP="/tmp/astro_quote_gateway.cjs"
CONTAINER_PATH="${ASTRO_QUOTE_GATEWAY_PATH:-/home/ubuntu/astro-server/quote-gateway.cjs}"
DEPLOY_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_PATH="${CONTAINER_PATH}.bak-${DEPLOY_STAMP}"
LOCAL_SHA256="$(shasum -a 256 "$SOURCE" | awk '{print $1}')"

test -r "$SSH_KEY"
test -f "$SOURCE"
node --check "$SOURCE"
scp -q -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SOURCE" "$SSH_TARGET:$REMOTE_TMP"

BACKUP_RESULT="$(ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" \
  "set -e; if sudo -n docker exec '$CONTAINER' test -f '$CONTAINER_PATH'; then sudo -n docker exec '$CONTAINER' cp '$CONTAINER_PATH' '$BACKUP_PATH'; sudo -n docker exec '$CONTAINER' chmod 400 '$BACKUP_PATH'; printf '%s' '$BACKUP_PATH'; fi; sudo -n docker cp '$REMOTE_TMP' '$CONTAINER:$CONTAINER_PATH'; rm -f '$REMOTE_TMP'; sudo -n docker exec '$CONTAINER' chmod 500 '$CONTAINER_PATH'; sudo -n docker exec '$CONTAINER' node --check '$CONTAINER_PATH'")"

REMOTE_SHA256="$(ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" \
  "sudo -n docker exec '$CONTAINER' sha256sum '$CONTAINER_PATH'" | awk '{print $1}')"
if [[ "$REMOTE_SHA256" != "$LOCAL_SHA256" ]]; then
  printf 'Quote gateway checksum mismatch: local=%s remote=%s\n' "$LOCAL_SHA256" "$REMOTE_SHA256" >&2
  exit 1
fi

if [[ -n "$BACKUP_RESULT" ]]; then
  printf 'Quote gateway backup: %s\n' "$BACKUP_RESULT"
fi
printf 'Quote gateway SHA256: %s\n' "$REMOTE_SHA256"

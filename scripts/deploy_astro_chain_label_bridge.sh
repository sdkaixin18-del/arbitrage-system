#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_KEY="${ASTRO_CHAIN_LABEL_SSH_KEY:-${ASTRO_MANUAL_ORDER_SSH_KEY:-/home/example/Downloads/astro.pem}}"
SSH_TARGET="${ASTRO_CHAIN_LABEL_SSH_TARGET:-${ASTRO_MANUAL_ORDER_SSH_TARGET:-ubuntu@192.0.2.10}}"
CONTAINER="${ASTRO_CHAIN_LABEL_CONTAINER:-${ASTRO_MANUAL_ORDER_CONTAINER:-astro-app}}"
DIST="${ASTRO_CHAIN_LABEL_DIST:-/home/ubuntu/astro-admin/dist}"
BRIDGE="$ROOT/scripts/astro_chain_label_bridge.js"
UPDATER="$ROOT/scripts/astro_chain_label_update.cjs"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_BRIDGE="/tmp/astro_chain_label_bridge-${STAMP}.js"
REMOTE_UPDATER="/tmp/astro_chain_label_update-${STAMP}.cjs"
BACKUP_DIR="/home/ubuntu/astro-chain-label-backups/${STAMP}"

test -r "$SSH_KEY"
test -f "$BRIDGE"
test -f "$UPDATER"
node --check "$BRIDGE"
node --check "$UPDATER"

scp -q -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$BRIDGE" "$SSH_TARGET:$REMOTE_BRIDGE"
scp -q -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$UPDATER" "$SSH_TARGET:$REMOTE_UPDATER"

ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" \
  "set -e; mkdir -p '$BACKUP_DIR'; sudo -n docker cp '$CONTAINER:$DIST/index.html' '$BACKUP_DIR/index.html'; for name in auto-chain-labels.js auto-chain-label-update.cjs auto-chain-labels.json; do if sudo -n docker exec '$CONTAINER' test -f '$DIST/'\"\$name\"; then sudo -n docker cp '$CONTAINER:$DIST/'\"\$name\" '$BACKUP_DIR/'\"\$name\"; fi; done; sudo -n docker cp '$REMOTE_BRIDGE' '$CONTAINER:$DIST/auto-chain-labels.js'; sudo -n docker cp '$REMOTE_UPDATER' '$CONTAINER:$DIST/auto-chain-label-update.cjs'; rm -f '$REMOTE_BRIDGE' '$REMOTE_UPDATER'; sudo -n docker exec '$CONTAINER' node -e \"const fs=require('fs');const p='$DIST/index.html';const tag='<script defer src=\\\"./auto-chain-labels.js\\\"></script>';let s=fs.readFileSync(p,'utf8');if(!s.includes('auto-chain-labels.js')){s=s.replace('</head>',tag+'</head>');fs.writeFileSync(p,s)}\"; sudo -n docker exec '$CONTAINER' sh -c \"test -f '$DIST/auto-chain-labels.json' || printf '%s\\n' '{\\\"version\\\":1,\\\"updatedAt\\\":0,\\\"items\\\":{}}' > '$DIST/auto-chain-labels.json'\"; sudo -n docker exec '$CONTAINER' chmod 644 '$DIST/auto-chain-labels.js' '$DIST/auto-chain-label-update.cjs' '$DIST/auto-chain-labels.json' '$DIST/index.html'; sudo -n docker exec '$CONTAINER' node --check '$DIST/auto-chain-labels.js'; sudo -n docker exec '$CONTAINER' node --check '$DIST/auto-chain-label-update.cjs'; sudo -n docker exec '$CONTAINER' grep -q 'auto-chain-labels.js' '$DIST/index.html'"

LOCAL_BRIDGE_SHA="$(shasum -a 256 "$BRIDGE" | awk '{print $1}')"
REMOTE_BRIDGE_SHA="$(ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" "sudo -n docker exec '$CONTAINER' sha256sum '$DIST/auto-chain-labels.js'" | awk '{print $1}')"
if [[ "$LOCAL_BRIDGE_SHA" != "$REMOTE_BRIDGE_SHA" ]]; then
  printf 'Astro chain label bridge checksum mismatch: local=%s remote=%s\n' "$LOCAL_BRIDGE_SHA" "$REMOTE_BRIDGE_SHA" >&2
  exit 1
fi

printf 'Astro chain label bridge backup: %s\n' "$BACKUP_DIR"
printf 'Astro chain label bridge SHA256: %s\n' "$REMOTE_BRIDGE_SHA"

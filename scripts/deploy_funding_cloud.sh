#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SSH_KEY=${FUNDING_CLOUD_SSH_KEY:-${ASTRO_MANUAL_ORDER_SSH_KEY:-/home/example/Downloads/astro.pem}}
SSH_TARGET=${FUNDING_CLOUD_SSH_TARGET:-${ASTRO_MANUAL_ORDER_SSH_TARGET:-ubuntu@192.0.2.10}}
RELEASE_ID=$(date -u +%Y%m%dT%H%M%SZ)
REMOTE_ROOT=/opt/astro-funding-cloud
REMOTE_RELEASE=$REMOTE_ROOT/releases/$RELEASE_ID
ARCHIVE=$(mktemp -t astro-funding-cloud.XXXXXX.tar.gz)
trap 'rm -f "$ARCHIVE"' EXIT HUP INT TERM
export COPYFILE_DISABLE=1

test -r "$SSH_KEY"

tar -C "$ROOT_DIR" -czf "$ARCHIVE" \
  --exclude='__pycache__' --exclude='*.pyc' \
  backend/app backend/funding-cloud-requirements.txt \
  scripts/funding_cloud_rpc.py scripts/import_funding_cloud_rows.py \
  scripts/astro-funding-cloud.service

scp -q -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  "$ARCHIVE" "$SSH_TARGET:/tmp/astro-funding-cloud-$RELEASE_ID.tar.gz"

ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" \
  "set -eu; \
   sudo -n install -d -o ubuntu -g ubuntu '$REMOTE_RELEASE' /var/lib/astro-funding-cloud; \
   sudo -n tar -xzf '/tmp/astro-funding-cloud-$RELEASE_ID.tar.gz' -C '$REMOTE_RELEASE'; \
   sudo -n chown -R ubuntu:ubuntu '$REMOTE_RELEASE'; \
   sudo -n find '$REMOTE_RELEASE' -type d -exec chmod 0755 {} +; \
   rm -f '/tmp/astro-funding-cloud-$RELEASE_ID.tar.gz'; \
   if [ ! -x '$REMOTE_ROOT/venv/bin/python' ]; then \
     sudo -n install -d -o ubuntu -g ubuntu '$REMOTE_ROOT'; \
     python3 -m venv '$REMOTE_ROOT/venv'; \
   fi; \
   '$REMOTE_ROOT/venv/bin/pip' install --disable-pip-version-check -q -r '$REMOTE_RELEASE/backend/funding-cloud-requirements.txt'; \
   sudo -n ln -sfn '$REMOTE_RELEASE' '$REMOTE_ROOT/current'; \
   sudo -n install -m 0644 '$REMOTE_RELEASE/scripts/astro-funding-cloud.service' /etc/systemd/system/astro-funding-cloud.service; \
   sudo -n systemctl daemon-reload; \
   sudo -n systemctl enable --now astro-funding-cloud.service; \
   sudo -n systemctl restart astro-funding-cloud.service"

for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" \
    "curl -fsS --max-time 3 http://127.0.0.1:8766/health"; then
    exit 0
  fi
  sleep 1
done

ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$SSH_TARGET" \
  "sudo -n systemctl status astro-funding-cloud.service --no-pager; sudo -n journalctl -u astro-funding-cloud.service -n 80 --no-pager"
exit 1

#!/bin/sh
set -eu
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEPTH_KEY=${ASTRO_DEPTH_CLOUD_SSH_KEY:-/home/example/Downloads/astro.pem}
DEPTH_TARGET=${ASTRO_DEPTH_CLOUD_SSH_TARGET:-ubuntu@192.0.2.10}
DEPTH_RELEASE=$(date -u +%Y%m%dT%H%M%SZ)
DEPTH_ARCHIVE=$(mktemp -t astro-depth-cloud.XXXXXX.tar.gz)
COPYFILE_DISABLE=1 tar --no-xattrs --no-acls -C "$ROOT_DIR" -czf "$DEPTH_ARCHIVE" --exclude='__pycache__' --exclude='*.pyc' backend/app scripts/astro-depth-cloud.service
scp -q -i "$DEPTH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$DEPTH_ARCHIVE" "$DEPTH_TARGET:/tmp/astro-depth-cloud-$DEPTH_RELEASE.tar.gz"
ssh -i "$DEPTH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$DEPTH_TARGET" \
  "set -eu; sudo install -d -o ubuntu -g ubuntu /opt/astro-depth-cloud /opt/astro-depth-cloud/releases/$DEPTH_RELEASE /var/lib/astro-depth-cloud; tar -xzf /tmp/astro-depth-cloud-$DEPTH_RELEASE.tar.gz -C /opt/astro-depth-cloud/releases/$DEPTH_RELEASE; if [ -L /opt/astro-depth-cloud/current ]; then readlink /opt/astro-depth-cloud/current; fi; ln -sfn /opt/astro-depth-cloud/releases/$DEPTH_RELEASE /opt/astro-depth-cloud/current; sudo install -m 0644 /opt/astro-depth-cloud/current/scripts/astro-depth-cloud.service /etc/systemd/system/astro-depth-cloud.service; sudo systemctl daemon-reload; sudo systemctl enable --now astro-depth-cloud.service; sudo systemctl restart astro-depth-cloud.service; systemctl is-active astro-depth-cloud.service"
printf 'Cloud depth release: %s\n' "$DEPTH_RELEASE"

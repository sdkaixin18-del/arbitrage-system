#!/usr/bin/env bash
set -euo pipefail

source_env="$1"
target_env="$2"
mode="${3:-preserve}"
case "$mode" in
  preserve|replace) ;;
  *) echo "Invalid runtime config mode" >&2; exit 2 ;;
esac
if [ -L "$target_env" ]; then
  echo "Refusing a symlink runtime configuration" >&2
  exit 1
fi
if [ -f "$target_env" ] && [ "$mode" = preserve ]; then
  chmod 600 "$target_env"
  exit 0
fi
if [ ! -f "$source_env" ]; then
  echo "Runtime configuration source is missing" >&2
  exit 1
fi
umask 077
mkdir -p "$(dirname "$target_env")"
if [ -f "$target_env" ]; then
  backup="$(mktemp "${target_env}.backup.XXXXXX")"
  install -m 600 "$target_env" "$backup"
fi
staged="$(mktemp "${target_env}.tmp.XXXXXX")"
trap 'rm -f "$staged"' EXIT
install -m 600 "$source_env" "$staged"
mv -f "$staged" "$target_env"

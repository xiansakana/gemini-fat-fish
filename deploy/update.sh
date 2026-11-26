#!/usr/bin/env bash
# 服务器自动更新：拉取 main，有变化就重启服务。
# 安装见 deploy/README.md
set -euo pipefail

APP_DIR="/srv/apps/gemini-fat-fish"
cd "$APP_DIR"

git fetch --quiet origin main
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

git pull --ff-only --quiet origin main
sudo -n systemctl restart gemini-fish.service gemini-fish-qq.service
echo "$(date -Is) updated ${LOCAL:0:8} -> ${REMOTE:0:8}"

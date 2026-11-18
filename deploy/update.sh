#!/usr/bin/env bash
# Обновление до свежей версии из GitHub и перезапуск. Запуск: /opt/shturman/deploy/update.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="${BRANCH:-$(git -C "$DIR" rev-parse --abbrev-ref HEAD)}"
cd "$DIR"
"$DIR/deploy/backup.sh"                     # копия базы перед обновлением
git fetch -q origin "$BRANCH"
git reset -q --hard "origin/$BRANCH"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps

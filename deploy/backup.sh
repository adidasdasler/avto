#!/usr/bin/env bash
# Резервная копия PostgreSQL в $DIR/backups, хранится 14 дней.
# Восстановление:  gunzip -c backups/<файл>.sql.gz | docker compose exec -T db psql -U shturman shturman
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$DIR/backups"
cd "$DIR"
if ! docker compose ps --status running db | grep -q db; then
  echo "$(date '+%F %T') база не запущена — копия пропущена"
  exit 0
fi
FILE="$DIR/backups/shturman-$(date +%F-%H%M).sql.gz"
umask 027  # копия базы — не для чужих глаз
docker compose exec -T db pg_dump -U shturman --clean --if-exists shturman | gzip > "$FILE.tmp"
chown 1000:1000 "$FILE.tmp"  # чтобы панель (пользователь app) могла восстанавливать и удалять
mv "$FILE.tmp" "$FILE"
find "$DIR/backups" -name 'shturman-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') копия: $FILE ($(du -h "$FILE" | cut -f1))"

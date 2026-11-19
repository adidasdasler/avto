#!/usr/bin/env bash
# Сотрудники панели администратора: ./deploy/admin.sh add ivan --role admin   (справка: ./deploy/admin.sh -h)
# Запускается на сервере из /opt/shturman; правит .env, показывает QR-код для приложения 2FA прямо в терминале.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Нет файла .env в $(pwd)"; exit 1; }
chmod 600 .env
exec docker compose run --rm --no-deps -u root -e ENV_FILE=/app/.env -v "$PWD/.env:/app/.env" \
  api python -m shturman.admin.cli "$@"

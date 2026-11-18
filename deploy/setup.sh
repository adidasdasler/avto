#!/usr/bin/env bash
# Первичная настройка чистого сервера Ubuntu 22.04/24.04 под Штурман Авто.
# Запуск от root:  curl -fsSL https://raw.githubusercontent.com/adidasdasler/avto/claude/wizardly-heisenberg-b4kry4/deploy/setup.sh | bash
# Скрипт можно запускать повторно — он ничего не ломает.
set -euo pipefail

REPO="${REPO:-https://github.com/adidasdasler/avto.git}"
BRANCH="${BRANCH:-claude/wizardly-heisenberg-b4kry4}"
DIR="${DIR:-/opt/shturman}"

[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "==> Обновление системы и пакеты"
apt-get update -q
apt-get -y -q upgrade
apt-get install -y -q ca-certificates curl git ufw openssl cron

echo "==> Docker (из репозитория Ubuntu)"
if ! command -v docker >/dev/null; then
  apt-get install -y -q docker.io
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -q docker-compose-v2 || apt-get install -y -q docker-compose-plugin
fi
# Зеркало Docker Hub на случай, если он недоступен из дата-центра
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  echo '{"registry-mirrors": ["https://mirror.gcr.io"]}' > /etc/docker/daemon.json
fi
systemctl enable --now docker
systemctl restart docker

echo "==> Файл подкачки 2 ГБ"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
fi

echo "==> Файрвол: SSH, HTTP, HTTPS"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

echo "==> Код проекта в $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
chmod +x "$DIR"/deploy/*.sh

if [ ! -f "$DIR/.env" ]; then
  echo "==> Создаю $DIR/.env из шаблона"
  cp "$DIR/.env.example" "$DIR/.env"
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" "$DIR/.env"
  chmod 600 "$DIR/.env"
fi

echo "==> Ежедневная резервная копия базы (03:30)"
echo "30 3 * * * root $DIR/deploy/backup.sh >> /var/log/shturman-backup.log 2>&1" > /etc/cron.d/shturman-backup

IP=$(curl -fsS4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
cat <<MSG

Готово. Дальше:
  1. В DNS домена создайте A-записи  @ и www  →  $IP
  2. Заполните токены:  nano $DIR/.env
     (AVC_TOKEN, MAX_BOT_TOKEN, MAX_BOT_URL, ROBOKASSA_*; PUBLIC_BASE_URL=https://ваш-домен)
  3. Запуск:  cd $DIR && docker compose up -d --build
  4. Проверка: docker compose ps   и   docker compose logs -f max-bot
MSG

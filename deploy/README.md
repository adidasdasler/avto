# Запуск на сервере (vincv.ru)

Связка: PostgreSQL + сайт/API + MAX-бот за Caddy (автоматический HTTPS от Let's Encrypt).
Всё в Docker Compose, данные базы и сертификаты — в томах Docker.

## 1. DNS
У регистратора домена создайте две A-записи на IP сервера:

| Имя | Тип | Значение |
|---|---|---|
| `@` (vincv.ru) | A | IP сервера |
| `www` | A | IP сервера |

Записи расходятся от нескольких минут до пары часов. Проверка: `ping vincv.ru` должен показать IP сервера.

## 2. Первичная настройка (один раз)
Подключитесь к серверу по SSH под root и выполните:
```bash
curl -fsSL https://raw.githubusercontent.com/adidasdasler/avto/claude/wizardly-heisenberg-b4kry4/deploy/setup.sh | bash
```
Скрипт обновит систему, поставит Docker, включит подкачку 2 ГБ и файрвол (22, 80, 443),
скачает проект в `/opt/shturman`, создаст `.env` со случайным паролем базы и включит
ежедневную копию базы в 03:30.

## 3. Настройки
```bash
nano /opt/shturman/.env
```
Обязательно:
```
DOMAIN=vincv.ru
PUBLIC_BASE_URL=https://vincv.ru
PROVIDER_MODE=live
AVC_TOKEN=...
MAX_BOT_TOKEN=...
MAX_BOT_URL=https://max.ru/<имя_бота>
ROBOKASSA_LOGIN=...
ROBOKASSA_PASSWORD1=...
ROBOKASSA_PASSWORD2=...
ROBOKASSA_TEST=true
DEV_FAKE_PAYMENTS=false
```
`DATABASE_URL` в `.env` трогать не нужно — в Docker адрес базы задаётся в `docker-compose.yml`.

## 4. Запуск
```bash
cd /opt/shturman && docker compose up -d --build
```
Первая сборка — несколько минут. Проверка:
```bash
docker compose ps
```
```bash
docker compose logs -f max-bot
```
Сайт: https://vincv.ru — сертификат Caddy получит сам при первом заходе (нужны DNS-записи из шага 1).

## 5. Robokassa
В личном кабинете → Технические настройки:

| Поле | Значение | Метод |
|---|---|---|
| Result URL | `https://vincv.ru/pay/robokassa/result` | POST |
| Success URL | `https://vincv.ru/pay/robokassa/success` | GET |
| Fail URL | `https://vincv.ru/pay/robokassa/fail` | GET |

Алгоритм расчёта хеша — тот же, что `ROBOKASSA_HASH` в `.env` (по умолчанию MD5).

## Обслуживание
| Что | Команда |
|---|---|
| Обновить до свежей версии | `/opt/shturman/deploy/update.sh` (сначала делает копию базы) |
| Логи | `docker compose logs -f api` / `max-bot` / `caddy` |
| Перезапуск после правки `.env` | `docker compose up -d` |
| Копия базы вручную | `/opt/shturman/deploy/backup.sh` (файлы в `/opt/shturman/backups`, 14 дней) |
| Восстановить из копии | `gunzip -c backups/<файл>.sql.gz \| docker compose exec -T db psql -U shturman shturman` |
| Telegram-бот (когда понадобится) | `docker compose --profile telegram up -d` |

Копии лежат на том же сервере. Раз в неделю скачивайте свежую к себе:
`scp root@vincv.ru:/opt/shturman/backups/<файл>.sql.gz .`

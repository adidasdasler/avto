# Штурман Авто — MVP (лендинг + боты MAX и Telegram)

Проверка автомобиля по VIN, госномеру или ссылке на объявление на данных API
[avtovincod.ru](https://avtovincod.ru). Реализует этап 1–2 спецификации «Штурман Авто — спецификация MVP»:
лендинг с бесплатной экспресс-проверкой, Telegram-бот, каскад запросов к провайдеру по тарифам,
RedactionLayer, кэш по VIN, PDF-отчёт и биллинг (разовые отчёты, пакеты, безлимит на сутки).

## Быстрый старт (без токена, на офлайн-образцах)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn shturman.api.app:get_app --factory --reload   # http://localhost:8000
pytest
```

`PROVIDER_MODE=mock` (по умолчанию) отдаёт без сети настоящие ответы avtovincod.ru на тестовые
значения (`shturman/provider/samples/`). Пример полного отчёта — `/example`, PDF — `python scripts/preview_report.py`.

## Подключение avtovincod.ru

1. Зарегистрироваться в личном кабинете avtovincod.ru и получить токен.
2. В `.env`: `PROVIDER_MODE=live`, `AVC_TOKEN=...`.
3. Обновить образцы ответов (бесплатно, тестовые значения работают даже при нулевом балансе):
   ```bash
   python scripts/dump_test_responses.py
   ```
   Ответы лягут в `tests/fixtures/live/`; при изменении схемы скопируйте их в `shturman/provider/samples/`
   и поправьте анализаторы в `shturman/rules.py`.

Тестовые значения провайдера: VIN `X7TEST00000000001`, госномер `Т000ТТ00`, кузов `TEST-0000001`,
СТС `0000000000`. По ним любой метод отвечает образцом с `"testMode": true` и `billing.charged = 0`.

## Тарифы и каскад

| Тариф | Методы avtovincod.ru |
|---|---|
| Экспресс (0 ₽, лимит в сутки) | `decode`, `pledge`, `taxi`, `elpts`, `accidents` (+ `gos2vin` для госномера) |
| Базовый | + `score`, `brief`, `probeg`, `price`, `fines`* |
| Досье | + `vin`, `osago-history`, `offers`, `plate-history`, `pros-cons`, `nomerogram`, `recalls`, `customs`*, `lizing`*, `lawsuits`*, `sharing`* |
| Досье+ | + `accidents-rt` (асинхронный, с опросом `task`), `service`* |

\* гейты — дорогие методы только по условию: штрафы — если `/brief` вернул СТС; таможня — VIN не
отечественной сборки (не X/Z); лизинг — владелец-юрлицо или авто моложе 7 лет; суды — есть залог
или ограничения; каршеринг — моложе 6 лет; сервисная история — моложе 10 лет.
Набор методов меняется в `shturman/cascade.py`, цены — в `.env`.

При повышении тарифа уже полученные разделы не запрашиваются повторно; ответы кэшируются
по VIN с TTL по типу источника (`shturman/provider/methods.py`). Ошибки провайдера
(`NOT_ALLOWED`, сеть и т. п.) не роняют отчёт — раздел помечается «нет данных».
Расходы по блоку `billing` пишутся в таблицу `provider_log`.

## Светофор

`shturman/rules.py`: каждый метод разбирается в раздел со статусом, из статусов складывается светофор.

- 🔴 **Критично** — активный залог, розыск, ограничения ГИБДД, действующий лизинг, уменьшение пробега
  по техосмотрам/сервису, ограничения в ЭПТС, прекращённая регистрация, 2+ ДТП.
- 🟡 **Внимание** — такси (разрешение или ОСАГО «такси»), ДТП, неоплаченные штрафы, каршеринг, суды,
  завершённый лизинг, отзывные кампании, пробег в объявлении меньше ранее зафиксированного, 4+ владельцев.
- 🟢 **В порядке** — остальное.

Балл 0–100: из 100 вычитается вес каждого риска (по умолчанию 30 за красный, 10 за жёлтый, у отдельных
правил свой вес). Светофор показывается в PDF, на лендинге и в сообщениях бота.

## Персональные данные

`shturman/redaction.py` удаляет поля с ПДн (ФИО, дата рождения, паспорт, телефоны, e-mail,
залогодатель, должник…) и маскирует ФИО/телефоны/e-mail в тексте **до** записи в кэш и БД.
Хостинг — VPS в РФ (152-ФЗ).

## MAX-бот и оплата через Robokassa

```bash
python -m shturman.bot.max
```

Сценарий тот же, что в Telegram: `/start` → роль → VIN/госномер/ссылка → экспресс-результат со светофором →
кнопка тарифа → ссылка на оплату Robokassa (карта, СБП) → отчёт PDF приходит в чат
(на Windows без GTK — HTML). Подборщики могут прислать список VIN, команды `/balance`, `/history`,
`/tariffs`, `/help`. Клиент MAX Bot API — `shturman/bot/max_api.py` (сверен с SDK maxapi 1.2.4).

Оплата подтверждается двумя путями (`shturman/orders.py`):
1. **Уведомление Robokassa** на `https://<ваш-домен>/pay/robokassa/result` (Result URL в ЛК, метод POST) —
   нужен публичный сервер. Success URL — `/pay/robokassa/success`, Fail URL — `/pay/robokassa/fail`.
2. **Опрос статуса счёта** (OpStateExt) каждые 15 с, а также по кнопке «Я оплатил — проверить» —
   работает и на домашнем компьютере, без публичного адреса (`ROBOKASSA_POLL_STATUS=true`).

Чек по 54-ФЗ передаётся в параметре `Receipt` (`ROBOKASSA_RECEIPT=true`), для самозанятых —
подключите «Робочеки» в ЛК. Для разработки без платёжки: `DEV_FAKE_PAYMENTS=true`.

## Telegram-бот

```bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_BOT_USERNAME=... python -m shturman.bot
```

`/start` → выбор роли (разово / подборщик + статус физлицо/ИП/самозанятый/ООО) → VIN/госномер/ссылка
→ экспресс-результат с кнопками тарифов → оплата Telegram Payments (ЮКасса/СБП,
`TELEGRAM_PAYMENT_TOKEN`) → PDF. Подборщик может прислать список VIN (по строке на каждый):
при наличии пакета/безлимита сразу приходят PDF «Досье». Команды: `/balance`, `/history`, `/tariffs`, `/help`.
Кнопка «Полный отчёт» на лендинге ведёт в бот по deep-link `?start=chk_<id>`.
Для разработки без платёжки: `DEV_FAKE_PAYMENTS=true`.

## Деплой

Сервер с доменом vincv.ru: пошагово в [deploy/README.md](deploy/README.md) — DNS, `deploy/setup.sh`
(Docker, подкачка, файрвол, ежедневная копия базы), `.env`, `docker compose up -d --build`,
адреса для Robokassa. Caddy сам получает HTTPS-сертификат. Обновление — `deploy/update.sh`.

## Структура

```
shturman/
  provider/   клиент avtovincod.ru, каталог методов, офлайн-образцы
  identifiers.py  разбор VIN / госномера / кузова / ссылки
  redaction.py    RedactionLayer
  cascade.py      каскад по тарифам, гейты, кэш
  rules.py        «светофор»
  report/         HTML-шаблон и PDF (WeasyPrint)
  billing.py      тарифы, пакеты, безлимит, бесплатный лимит
  services.py     общее ядро для сайта и ботов
  api/app.py      FastAPI: лендинг, /api/check, /r/<id>[.pdf], /example
  bot/            Telegram-бот (aiogram 3)
landing/index.html
scripts/dump_test_responses.py
```

## Что дальше

- Сверить схему ответов на настоящих тестовых значениях (см. выше).
- MAX-бот (этап 3): ядро `CheckService` не зависит от платформы — нужен только адаптер на `maxapi`.
- Счета и акты для ИП/ООО, white-label отчёты, миграции Alembic вместо `create_all`.
- Извлечение VIN из страницы объявления (сейчас VIN/госномер ищутся только в самой ссылке).

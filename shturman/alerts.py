"""Мониторинг расхода у провайдера и тревоги сотрудникам.

Главная проверка — «чужой расход». Если токен avtovincod.ru утёк, вор тратит депозит своими
запросами, и в нашем журнале (ProviderLog) их нет. Но баланс у провайдера приходит в каждом ответе:
сравниваем, на сколько уменьшился баланс за окно, и сколько за это время списали наши запросы.
Разница больше порога — токеном пользуется кто-то ещё. Чтобы баланс был свежим даже без трафика,
раз в BALANCE_PROBE_MINUTES делаем бесплатный запрос по тестовому VIN (тестовые значения не тарифицируются).

Тревоги: в панели (сводка) и личным сообщением сотрудникам в MAX и Telegram.
Одна и та же тревога повторяется не чаще ALERT_REPEAT_HOURS.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select

from .config import Settings
from .db import Database, ProviderLog, SystemAlert, utcnow
from .net import ssl_context
from .provider import Provider, ProviderError
from .provider.methods import TEST_VALUES

log = logging.getLogger(__name__)


@dataclass
class Alert:
    key: str
    level: str  # critical | warning
    text: str


async def _balances(s, since: datetime) -> list[tuple[int, float, datetime]]:
    rows = await s.execute(select(ProviderLog.id, ProviderLog.balance, ProviderLog.created_at)
                           .where(ProviderLog.balance.is_not(None), ProviderLog.created_at >= since)
                           .order_by(ProviderLog.id))
    return [(i, float(b), t) for i, b, t in rows.all()]


async def external_spend(db: Database, window: timedelta) -> float | None:
    """На сколько баланс у провайдера уменьшился больше, чем списали наши запросы за окно.

    Считаем по крайним точкам окна, а не по парам соседних записей: параллельные запросы
    логируются не по порядку списаний, и попарное сравнение давало бы ложные тревоги.
    Отрицательное значение — было пополнение; None — мало данных."""
    async with db.session() as s:
        points = await _balances(s, utcnow() - window)
        if len(points) < 2:
            return None
        (first_id, first_bal, _), (last_id, last_bal, _) = points[0], points[-1]
        ours = (await s.execute(select(func.coalesce(func.sum(ProviderLog.charged), 0)).where(
            ProviderLog.id > first_id, ProviderLog.id <= last_id))).scalar_one()
    return round((first_bal - last_bal) - float(ours), 2)


async def evaluate(db: Database, settings: Settings) -> list[Alert]:
    out: list[Alert] = []
    if settings.provider_mode != "live":  # в офлайн-режиме баланс и расход ненастоящие
        return out
    now = utcnow()
    for window, label in ((timedelta(hours=1), "за час"), (timedelta(hours=24), "за сутки")):
        ext = await external_spend(db, window)
        if ext is not None and ext > settings.alert_external_spend_rub:
            out.append(Alert("token_leak", "critical",
                             f"С баланса avtovincod.ru {label} ушло на {ext:.0f} ₽ больше, чем потратили наши "
                             f"запросы. Возможно, токен утёк: перевыпустите его в личном кабинете avtovincod.ru "
                             f"и обновите AVC_TOKEN в .env."))
            break
    async with db.session() as s:
        def spent(since):
            return select(func.coalesce(func.sum(ProviderLog.charged), 0)).where(ProviderLog.created_at >= since)
        hour = float((await s.execute(spent(now - timedelta(hours=1)))).scalar_one())
        day = float((await s.execute(spent(now.replace(hour=0, minute=0, second=0, microsecond=0)))).scalar_one())
        errors = (await s.execute(select(func.count()).select_from(ProviderLog).where(
            ProviderLog.error.is_not(None), ProviderLog.created_at >= now - timedelta(hours=1)))).scalar_one()
        last = (await s.execute(select(ProviderLog.balance).where(ProviderLog.balance.is_not(None))
                                .order_by(ProviderLog.id.desc()).limit(1))).scalar_one_or_none()
    if hour > settings.alert_spend_hour_rub:
        out.append(Alert("spend_hour", "warning", f"Расход у провайдера за последний час — {hour:.0f} ₽ "
                                                  f"(порог {settings.alert_spend_hour_rub:.0f} ₽). Проверьте, нет ли накрутки."))
    if day > settings.alert_spend_day_rub:
        out.append(Alert("spend_day", "warning", f"Расход у провайдера за сегодня — {day:.0f} ₽ "
                                                 f"(порог {settings.alert_spend_day_rub:.0f} ₽)."))
    if last is not None and float(last) < settings.alert_balance_below_rub:
        out.append(Alert("balance_low", "warning", f"На балансе avtovincod.ru осталось {float(last):.0f} ₽ — "
                                                   f"пополните, иначе проверки перестанут работать."))
    if errors >= settings.alert_errors_per_hour:
        out.append(Alert("provider_errors", "warning", f"Ошибок источников за час: {errors}. "
                                                       f"Проверьте сводку панели и логи."))
    return out


async def record_new(db: Database, settings: Settings, alerts: list[Alert]) -> list[Alert]:
    """Сохраняет тревоги, которых не было последние ALERT_REPEAT_HOURS; возвращает новые для рассылки."""
    fresh = []
    since = utcnow() - timedelta(hours=settings.alert_repeat_hours)
    async with db.session() as s:
        for a in alerts:
            seen = (await s.execute(select(SystemAlert.id).where(SystemAlert.key == a.key,
                                                                 SystemAlert.created_at >= since))).first()
            if not seen:
                s.add(SystemAlert(key=a.key, level=a.level, message=a.text))
                fresh.append(a)
        await s.commit()
    return fresh


async def recent(db: Database, hours: int = 24) -> list[SystemAlert]:
    async with db.session() as s:
        rows = await s.execute(select(SystemAlert).where(SystemAlert.created_at >= utcnow() - timedelta(hours=hours))
                               .order_by(SystemAlert.id.desc()).limit(20))
        return list(rows.scalars())


def _ids(value: str) -> list[int]:
    return [int(x) for x in (value or "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]


async def notify_staff(db: Database, settings: Settings, alerts: list[Alert],
                       transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Личные сообщения сотрудникам: ALERT_MAX_USER_IDS (MAX) и ALERT_TG_CHAT_IDS (Telegram) из .env."""
    if not alerts:
        return 0
    text = "\n\n".join(("🚨 " if a.level == "critical" else "⚠️ ") + a.text for a in alerts)
    text = f"<b>{settings.brand_name}: тревога</b>\n\n{text}"
    sent = 0
    async with httpx.AsyncClient(timeout=15, verify=ssl_context(), transport=transport) as http:
        targets = ([("max", i) for i in _ids(settings.alert_max_user_ids)] if settings.max_bot_token else []) + \
                  ([("tg", i) for i in _ids(settings.alert_tg_chat_ids)] if settings.telegram_bot_token else [])
        for kind, ident in targets:
            try:
                if kind == "max":
                    r = await http.post(settings.max_api_url.rstrip("/") + "/messages", params={"user_id": ident},
                                        headers={"Authorization": settings.max_bot_token},
                                        json={"text": text[:4000], "format": "html"})
                else:
                    r = await http.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                                        json={"chat_id": ident, "text": text[:4000], "parse_mode": "HTML"})
                sent += r.status_code < 400
            except httpx.HTTPError as e:
                log.warning("Не удалось отправить тревогу (%s %s): %s", kind, ident, e)
    return sent


async def probe_balance(db: Database, provider: Provider) -> float | None:
    """Бесплатный запрос по тестовому VIN — только чтобы узнать текущий баланс у провайдера."""
    try:
        resp = await provider.call("decode", vin=TEST_VALUES["vin"])
    except ProviderError as e:
        log.warning("Проверка баланса не удалась: %s", e)
        return None
    if not isinstance(resp.balance, (int, float)):
        return None
    async with db.session() as s:
        s.add(ProviderLog(method="balance-probe", charged=resp.charged, test_mode=resp.test_mode,
                          balance=resp.balance))
        await s.commit()
    return float(resp.balance)


async def run(db: Database, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> list[Alert]:
    fresh = await record_new(db, settings, await evaluate(db, settings))
    for a in fresh:
        log.warning("ТРЕВОГА [%s] %s", a.key, a.text)
    await notify_staff(db, settings, fresh, transport)
    return fresh

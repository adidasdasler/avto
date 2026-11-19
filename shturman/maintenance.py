"""Регулярная очистка, чтобы база не росла без пользы.

Хранятся: проверки пользователей (данные отчёта ~3–15 КБ, PDF собирается заново при открытии),
заказы, оплаты, анкеты. Удаляются: истёкший кэш провайдера, анонимные экспресс-проверки
с сайта старше ANON_CHECKS_RETENTION_DAYS, старые токены входа и сессии, логи провайдера старше года.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select

from .auth import AuthService
from .config import Settings
from .db import CacheEntry, Check, Database, Order, Payment, ProviderLog, SystemAlert, utcnow

log = logging.getLogger(__name__)


async def cleanup(db: Database, settings: Settings) -> dict[str, int]:
    now = utcnow()
    out: dict[str, int] = {}
    async with db.session() as s:
        r = await s.execute(delete(CacheEntry).where(CacheEntry.expires_at < now))
        out["cache"] = r.rowcount or 0
        referenced = select(Order.check_id).where(Order.check_id.is_not(None)).union(
            select(Payment.check_id).where(Payment.check_id.is_not(None)))
        r = await s.execute(delete(Check).where(
            Check.user_id.is_(None), Check.tier == "express", Check.source != "example",
            Check.created_at < now - timedelta(days=settings.anon_checks_retention_days),
            Check.id.not_in(referenced)))
        out["anon_checks"] = r.rowcount or 0
        r = await s.execute(delete(ProviderLog).where(ProviderLog.created_at < now - timedelta(days=365)))
        out["provider_log"] = r.rowcount or 0
        await s.execute(delete(SystemAlert).where(SystemAlert.created_at < now - timedelta(days=90)))
        await s.commit()
    await AuthService(db).cleanup()
    log.info("Очистка: %s", out)
    return out

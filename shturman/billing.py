"""Тарифы, продукты и права доступа (бесплатный лимит, разовые отчёты, пакеты)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db import Check, Payment, User, utcnow


@dataclass(frozen=True)
class Product:
    code: str
    title: str
    price: int  # ₽
    tier: str | None = None  # разовый отчёт этого уровня
    credits: int = 0  # пакет: отчёты уровня «Досье»
    unlimited_hours: int = 0
    description: str = ""


def products(s: Settings) -> dict[str, Product]:
    items = [
        Product("basic", "Базовый отчёт", s.price_basic, tier="basic",
                description="Ограничения, штрафы, краткие данные, пробег, оценка"),
        Product("dossier", "Досье", s.price_dossier, tier="dossier",
                description="+ владельцы, ОСАГО, объявления, номера, фото, лизинг/суды/таможня"),
        Product("dossier_plus", "Досье+", s.price_dossier_plus, tier="dossier_plus",
                description="+ ДТП по базе ГИБДД в реальном времени, сервисная история"),
        Product("pack_10", "Пакет 10 отчётов", s.price_pack_10, credits=10, description="10 отчётов «Досье»"),
        Product("pack_25", "Пакет 25 отчётов", s.price_pack_25, credits=25, description="25 отчётов «Досье»"),
        Product("pack_50", "Пакет 50 отчётов", s.price_pack_50, credits=50, description="50 отчётов «Досье»"),
    ]
    return {p.code: p for p in items}


def has_unlimited(user: User | None, now: datetime | None = None) -> bool:
    return bool(user and user.unlimited_until and user.unlimited_until > (now or utcnow()))


def _today_checks():
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return select(func.count()).select_from(Check).where(Check.created_at >= start, Check.tier == "express",
                                                         Check.source != "example")


async def free_checks_used_today(session: AsyncSession, *, user_id: int | None = None,
                                 client_ip: str | None = None) -> int:
    """Экспресс-проверки за сутки: по аккаунту, а без аккаунта — по IP."""
    q = _today_checks()
    if user_id is not None:
        q = q.where(Check.user_id == user_id)
    elif client_ip is not None:
        q = q.where(Check.client_ip == client_ip, Check.user_id.is_(None))
    else:
        return 0
    return int((await session.execute(q)).scalar_one())


async def free_checks_from_ip_today(session: AsyncSession, client_ip: str) -> int:
    """Все экспресс-проверки с IP, со входом и без: защита от «ферм» аккаунтов."""
    return int((await session.execute(_today_checks().where(Check.client_ip == client_ip))).scalar_one())


async def free_checks_total_today(session: AsyncSession) -> int:
    return int((await session.execute(_today_checks())).scalar_one())


def can_use_credit(user: User | None, tier: str) -> bool:
    """Отчёт из пакета покрывает уровни до «Досье» включительно.
    Безлимит больше не продаётся, но ранее купленный действует до конца срока."""
    if tier not in ("basic", "dossier") or user is None:
        return False
    return has_unlimited(user) or user.credits > 0


async def take_credit(session: AsyncSession, user_id: int, tier: str) -> str:
    """Атомарно списывает отчёт из пакета: «уменьшить на 1, только если больше нуля» одним запросом.
    Параллельные запросы не смогут потратить один отчёт дважды."""
    if tier not in ("basic", "dossier"):
        raise PermissionError("Пакет покрывает отчёты до уровня «Досье»")
    user = await session.get(User, user_id)
    if has_unlimited(user):
        return "unlimited"
    res = await session.execute(update(User).where(User.id == user_id, User.credits > 0)
                                .values(credits=User.credits - 1))
    if res.rowcount != 1:
        raise PermissionError("Нет доступных отчётов в пакете")
    return "credit"


async def add_credits(session: AsyncSession, user_id: int, n: int) -> None:
    await session.execute(update(User).where(User.id == user_id).values(credits=User.credits + n))


async def apply_payment(session: AsyncSession, user: User, product: Product, *, check_id: str | None,
                        charge_id: str | None, provider: str = "telegram") -> Payment:
    """Регистрирует оплату и начисляет отчёты из пакета. Идемпотентно по charge_id."""
    if charge_id:
        existing = (await session.execute(
            select(Payment).where(Payment.provider_charge_id == charge_id))).scalar_one_or_none()
        if existing:
            return existing
    if product.credits:
        await add_credits(session, user.id, product.credits)  # атомарно, без потери параллельных оплат
    if product.unlimited_hours:
        base = user.unlimited_until if has_unlimited(user) else utcnow()
        user.unlimited_until = base + timedelta(hours=product.unlimited_hours)
    pay = Payment(user_id=user.id, product=product.code, check_id=check_id, amount_rub=product.price,
                  provider=provider, provider_charge_id=charge_id)
    session.add(pay)
    await session.flush()
    return pay

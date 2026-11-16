"""Тарифы, продукты и права доступа (бесплатный лимит, пакеты, безлимит на сутки)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
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
        Product("unlimited_day", "Безлимит на сутки", s.price_unlimited_day, unlimited_hours=24,
                description="Неограниченные отчёты «Досье» 24 часа"),
    ]
    return {p.code: p for p in items}


def has_unlimited(user: User | None, now: datetime | None = None) -> bool:
    return bool(user and user.unlimited_until and user.unlimited_until > (now or utcnow()))


async def free_checks_used_today(session: AsyncSession, *, user_id: int | None = None,
                                 client_ip: str | None = None) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.count()).select_from(Check).where(Check.created_at >= start)
    if user_id is not None:
        q = q.where(Check.user_id == user_id)
    elif client_ip is not None:
        q = q.where(Check.client_ip == client_ip, Check.user_id.is_(None))
    else:
        return 0
    return int((await session.execute(q)).scalar_one())


def can_use_credit(user: User | None, tier: str) -> bool:
    """Кредит из пакета или безлимит покрывают отчёты до уровня «Досье» включительно."""
    if tier not in ("basic", "dossier") or user is None:
        return False
    return has_unlimited(user) or user.credits > 0


def consume_credit(user: User, tier: str) -> str:
    if has_unlimited(user):
        return "unlimited"
    if user.credits > 0 and tier in ("basic", "dossier"):
        user.credits -= 1
        return "credit"
    raise PermissionError("Нет доступных отчётов в пакете")


async def apply_payment(session: AsyncSession, user: User, product: Product, *, check_id: str | None,
                        charge_id: str | None, provider: str = "telegram") -> Payment:
    """Регистрирует оплату и начисляет кредиты/безлимит. Идемпотентно по charge_id."""
    if charge_id:
        existing = (await session.execute(
            select(Payment).where(Payment.provider_charge_id == charge_id))).scalar_one_or_none()
        if existing:
            return existing
    if product.credits:
        user.credits += product.credits
    if product.unlimited_hours:
        base = user.unlimited_until if has_unlimited(user) else utcnow()
        user.unlimited_until = base + timedelta(hours=product.unlimited_hours)
    pay = Payment(user_id=user.id, product=product.code, check_id=check_id, amount_rub=product.price,
                  provider=provider, provider_charge_id=charge_id)
    session.add(pay)
    await session.flush()
    return pay

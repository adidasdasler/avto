"""152-ФЗ: согласия, выгрузка своих данных и удаление аккаунта.

Согласия хранятся двумя способами: действующее — в полях пользователя (pd_consent_at, marketing_consent_at),
а каждая выдача и каждый отзыв — в журнале ConsentLog с редакцией документа, источником, IP и браузером.

Удаление аккаунта: персональные данные стираются сразу (почта, пароль, привязки Яндекса и мессенджеров,
имя, анкета специалиста, сессии), проверки без оплат удаляются, проверки с оплатами обезличиваются.
Строка пользователя без персональных данных остаётся, потому что на неё ссылаются оплаты: их хранят
5 лет по закону о бухучёте и налоговому кодексу. Журнал согласий тоже остаётся (подтверждение законности
обработки до отзыва).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update

from .db import (Check, ConsentLog, Database, LoginToken, Order, Payment, ProProfile, ProStat, User, WebSession,
                 utcnow)

# Редакция документов. Меняется при изменении текстов /consent и /privacy — тогда все заново дают согласие.
PD_VERSION = "2026-11-19"
MARKETING_VERSION = "2026-11-19"
PUBLIC_VERSION = "2026-11-19"
OFFER_VERSION = "2026-11-19"
VERSIONS = {"pd": PD_VERSION, "marketing": MARKETING_VERSION, "public": PUBLIC_VERSION, "offer": OFFER_VERSION}
KIND_TITLES = {"pd": "Обработка персональных данных", "marketing": "Рекламные и информационные сообщения",
               "public": "Публикация анкеты (распространение данных)", "offer": "Условия оферты"}


def has_pd_consent(user: User | None) -> bool:
    return bool(user and user.pd_consent_at and user.pd_consent_version == PD_VERSION)


async def record(db: Database, user_id: int, kind: str, granted: bool, *, source: str,
                 ip: str | None = None, user_agent: str | None = None) -> None:
    if kind not in VERSIONS:
        raise ValueError(kind)
    now = utcnow()
    async with db.session() as s:
        s.add(ConsentLog(user_id=user_id, kind=kind, granted=granted, version=VERSIONS[kind], source=source,
                         ip=(ip or "")[:64] or None, user_agent=(user_agent or "")[:300] or None, created_at=now))
        user = await s.get(User, user_id)
        if kind == "pd":
            user.pd_consent_at, user.pd_consent_version = (now, PD_VERSION) if granted else (None, None)
        elif kind == "marketing":
            user.marketing_consent_at = now if granted else None
        await s.commit()


async def history(db: Database, user_id: int) -> list[ConsentLog]:
    async with db.session() as s:
        return list((await s.execute(select(ConsentLog).where(ConsentLog.user_id == user_id)
                                     .order_by(ConsentLog.id.desc()).limit(100))).scalars())


def _dt(v: datetime | None) -> str | None:
    return v.isoformat(timespec="seconds") + "Z" if v else None


async def export(db: Database, user_id: int) -> dict[str, Any]:
    """Все данные пользователя (ст. 14 152-ФЗ — право на доступ к своим данным)."""
    async with db.session() as s:
        u = await s.get(User, user_id)
        checks = (await s.execute(select(Check).where(Check.user_id == user_id).order_by(Check.created_at))).scalars()
        orders = (await s.execute(select(Order).where(Order.user_id == user_id).order_by(Order.id))).scalars()
        payments = (await s.execute(select(Payment).where(Payment.user_id == user_id).order_by(Payment.id))).scalars()
        profile = (await s.execute(select(ProProfile).where(ProProfile.user_id == user_id))).scalar_one_or_none()
        consents = (await s.execute(select(ConsentLog).where(ConsentLog.user_id == user_id)
                                    .order_by(ConsentLog.id))).scalars()
        return {
            "exported_at": _dt(utcnow()),
            "account": {"id": u.id, "email": u.email, "email_verified_at": _dt(u.email_verified_at),
                        "has_password": bool(u.password_hash), "yandex_linked": bool(u.yandex_id),
                        "max_id": u.max_id, "telegram_id": u.tg_id, "username": u.username, "role": u.role,
                        "legal_status": u.legal_status, "credits": u.credits, "created_at": _dt(u.created_at),
                        "pd_consent_at": _dt(u.pd_consent_at), "marketing_consent_at": _dt(u.marketing_consent_at)},
            "consents": [{"kind": c.kind, "granted": c.granted, "version": c.version, "source": c.source,
                          "ip": c.ip, "at": _dt(c.created_at)} for c in consents],
            "checks": [{"id": c.id, "query": c.query, "vin": c.vin, "plate": c.plate, "tier": c.tier,
                        "source": c.source, "created_at": _dt(c.created_at)} for c in checks],
            "orders": [{"id": o.id, "product": o.product, "amount_rub": o.amount_rub, "status": o.status,
                        "created_at": _dt(o.created_at), "paid_at": _dt(o.paid_at)} for o in orders],
            "payments": [{"id": p.id, "product": p.product, "amount_rub": p.amount_rub, "provider": p.provider,
                          "created_at": _dt(p.created_at)} for p in payments],
            "pro_profile": None if profile is None else {
                "name": profile.name, "kind": profile.kind, "city": profile.city, "about": profile.about,
                "services": profile.services, "phone": profile.phone, "telegram": profile.telegram,
                "max_link": profile.max_link, "website": profile.website, "status": profile.status,
                "consent_at": _dt(profile.consent_at)},
        }


async def delete_account(db: Database, user_id: int) -> dict[str, int]:
    """Удаляет персональные данные пользователя. Необратимо."""
    async with db.session() as s:
        user = await s.get(User, user_id, with_for_update=True)
        if user is None or user.deleted_at is not None:
            return {}
        paid = select(Order.check_id).where(Order.check_id.is_not(None)).union(
            select(Payment.check_id).where(Payment.check_id.is_not(None)))
        removed = (await s.execute(delete(Check).where(Check.user_id == user_id, Check.id.not_in(paid)))).rowcount
        kept = (await s.execute(update(Check).where(Check.user_id == user_id).values(user_id=None))).rowcount
        await s.execute(update(Order).where(Order.user_id == user_id).values(chat_id=None))
        profile = (await s.execute(select(ProProfile).where(ProProfile.user_id == user_id))).scalar_one_or_none()
        if profile is not None:
            await s.execute(delete(ProStat).where(ProStat.profile_id == profile.id))
            await s.delete(profile)
        await s.execute(delete(WebSession).where(WebSession.user_id == user_id))
        await s.execute(delete(LoginToken).where(LoginToken.user_id == user_id))
        now = utcnow()
        for attr in ("email", "password_hash", "yandex_id", "max_id", "tg_id", "username", "legal_status",
                     "email_verified_at", "totp_secret", "totp_enabled_at", "totp_last_counter", "recovery_codes",
                     "staff_role", "unlimited_until", "pd_consent_version", "pd_consent_at", "marketing_consent_at"):
            setattr(user, attr, None)
        user.credits, user.deleted_at = 0, now
        user.blocked_at, user.blocked_reason = now, "Аккаунт удалён по запросу пользователя"
        for kind in ("pd", "marketing"):
            s.add(ConsentLog(user_id=user_id, kind=kind, granted=False, version=VERSIONS[kind], source="delete",
                             created_at=now))
        await s.commit()
        return {"checks_deleted": removed, "checks_anonymized": kept, "profile": int(profile is not None)}


async def marketing_recipients(db: Database) -> list[User]:
    """Только для будущих рассылок: пользователи с действующим согласием на рекламу."""
    async with db.session() as s:
        return list((await s.execute(select(User).where(User.marketing_consent_at.is_not(None),
                                                        User.deleted_at.is_(None),
                                                        User.blocked_at.is_(None)))).scalars())

